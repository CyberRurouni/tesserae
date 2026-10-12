"""
interface/main.py — FastAPI wrapper for the Tesserae orchestrator.

Provides the REST API consumed by the Next.js frontend:
  - POST /api/runs              start a run (executes the orchestrator in background)
  - GET  /api/runs              list runs from the results directory
  - GET  /api/runs/{id}         run detail
  - GET  /api/runs/{id}/status  lightweight polling endpoint
  - GET  /api/config            read profile + last filter selection
  - POST /api/config            write the profile files

Run with:  python -m interface.main
"""
import asyncio
import json
import logging
import os
import socket
import sys
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from interface.profiles_api import router as profiles_router
from interface.heuristics_api import router as heuristics_router
from interface.orchestrate import _run_with_filters
from interface.select_filters import build_scraping_filters
from interface.verdicts_api import router as verdicts_router

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent

# Logs land here as well as on stdout. The file is the reliable one: stdout
# depends on how the process was started, and a piped or detached process
# shows nothing useful. Overridable so a test or a second instance can send
# them somewhere harmless.
LOG_FILE = Path(os.getenv("TESSERAE_LOG_FILE") or (ROOT / "logs" / "tesserae.log"))

DATA_DIR = ROOT / "data" / "ads"
PROFILE_DIR = ROOT / "data" / "profile"

app = FastAPI(title="Tesserae API", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# In-memory run state (polled by the frontend while a run is in flight)
run_states: dict[str, dict[str, Any]] = {}

TERMINAL_STATES = {"completed", "failed", "cancelled"}


# ── Schemas ────────────────────────────────────────────────────────────────

class RunRequest(BaseModel):
    """Filter selection coming from the frontend (non-technical form)."""

    category: str = "career_jobs"
    delivery_status: str = "active"
    platforms: Optional[Union[list[str], str]] = None
    media_type: str = "all"
    date_preset: str = "any"
    keywords: Optional[Union[list[str], str]] = None
    ad_age: Optional[Union[int, str]] = None
    regions: Optional[Union[list[str], str]] = "ALL"
    languages: Optional[Union[list[str], str]] = "en"
    sort_order: str = "relevance"
    run_mode: str = "generate"
    cycles: Optional[int] = None
    run_until_complete: bool = False
    heuristic_patterns: Optional[dict[str, list[str]]] = None
    # Which profile to run under. Omitted => the active profile, which is what
    # the current one-page config UI implies.
    profile_id: Optional[str] = None
    request_id: Optional[str] = None
    # Keywords crawled at once. 1 keeps the original sequential behaviour;
    # raise it for speed at the cost of captcha exposure.
    parallel_workers: int = 1

    @field_validator("keywords", mode="before")
    @classmethod
    def _split_keywords(cls, value):
        """Accept a JSON array, a JSON-array string, or a plain string.

        Keywords may contain spaces ("python developer"), so nothing is split
        on whitespace unless the caller sent a single bare token list.
        """
        if value is None:
            return None
        if isinstance(value, str):
            text = value.strip()
            if not text:
                return None
            if text.startswith("["):
                try:
                    parsed = json.loads(text)
                except json.JSONDecodeError:
                    parsed = None
                if isinstance(parsed, list):
                    return [str(k).strip() for k in parsed if str(k).strip()]
            return [text]
        if isinstance(value, list):
            return [str(k).strip() for k in value if str(k).strip()]
        return None

    @field_validator("platforms", "regions", "languages", mode="before")
    @classmethod
    def _as_list(cls, value):
        if value is None:
            return None
        if isinstance(value, list):
            return [str(v).strip() for v in value if str(v).strip()]
        text = str(value).strip()
        if not text:
            return None
        return [part.strip() for part in text.split(",") if part.strip()]

    @field_validator("ad_age", mode="before")
    @classmethod
    def _as_int(cls, value):
        if value in (None, "", "any"):
            return None
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return None
        return parsed if parsed > 0 else None


class ConfigRequest(BaseModel):
    about_us: Optional[str] = None
    additional_filters: Optional[str] = None


# ── Helpers ────────────────────────────────────────────────────────────────

def _find_run_file(run_id: str) -> Path | None:
    if not DATA_DIR.exists():
        return None
    for cat_dir in DATA_DIR.iterdir():
        if cat_dir.is_dir():
            candidate = cat_dir / f"{run_id}.json"
            if candidate.exists():
                return candidate
    return None


def _build_filters(request: RunRequest):
    """Build ScrapingFilters from a request without any interactive prompts.

    Everything the frontend does not send explicitly is filled with a sane
    default, so a background run can never block on stdin.
    """
    languages = request.languages or []
    if any(lang.lower() == "all" for lang in languages):
        languages = []

    regions = request.regions or ["ALL"]
    if any(region.upper() == "ALL" for region in regions):
        regions = ["ALL"]

    # The user's own sieve patterns join the category's built-ins for the run.
    # An explicit request may still add to these; neither replaces the other.
    from core.schemas.category import CATEGORIES, DEFAULT_CATEGORY_ID
    from modules import heuristics as heuristics_store

    category_heuristics = CATEGORIES.get(
        request.category, CATEGORIES[DEFAULT_CATEGORY_ID]
    ).heuristics.model_dump()
    base = heuristics_store.combined_for_run(category_heuristics)

    supplied = request.heuristic_patterns or {}
    heuristics = {
        field: sorted({*base.get(field, []), *supplied.get(field, [])})
        for field in heuristics_store.FIELDS
    }

    return build_scraping_filters(
        run_mode=request.run_mode,
        non_interactive=True,
        category=request.category,
        delivery_status=request.delivery_status,
        platforms=request.platforms,
        media_type=request.media_type,
        date_preset=request.date_preset,
        keywords=request.keywords,
        ad_age=request.ad_age,
        regions=regions,
        languages=languages,
        sort_order=request.sort_order,
        heuristic_patterns=heuristics,
    )


def _keyword_targets(request: RunRequest) -> int:
    if request.run_until_complete:
        return 0
    return max(1, request.cycles or 1)


app.include_router(profiles_router)
app.include_router(verdicts_router)
app.include_router(heuristics_router)


# ── Run lifecycle ──────────────────────────────────────────────────────────

async def run_orchestrator_background(run_id: str, request: RunRequest) -> None:
    """Execute the orchestrator, updating the polled run state as it goes."""
    state = run_states.setdefault(run_id, {"id": run_id})
    try:
        state.update(
            status="running",
            started_at=state.get("started_at") or datetime.now().isoformat(),
            error=None,
        )

        filters = _build_filters(request)
        total = len(filters.keywords) * _keyword_targets(request)
        state["keywords"] = list(filters.keywords)
        state["total_keywords"] = total
        state["progress"] = 5

        # Resolve the profile before the orchestrator so the run state reports
        # which lens it ran under. Accepted leads and universal junk are global
        # and need no profile; only this profile's own rejections do.
        from modules.profiles.active import (
            active_request_id,
            ensure_active_profile,
            read_active,
        )

        profile = await ensure_active_profile()
        if not request.profile_id:
            request = request.model_copy(update={"profile_id": profile.profile_id})
        # The search UI has no request picker, so a run arrives without one. Fall
        # back to the active request — without it the deferred bucket is skipped
        # and every parked ad gets judged again on the next run.
        if not request.request_id:
            active_request = active_request_id()
            if active_request:
                request = request.model_copy(update={"request_id": active_request})
        state["profile_id"] = profile.profile_id
        state["profile_about"] = profile.about
        state["request_id"] = request.request_id
        state["phase"] = "starting"
        state["harvested_total"] = 0
        state["keywords_harvested"] = []
        # A browser run is not abandoned by closing the tab, so say where the
        # evidence is before the fact rather than after it.
        state["log_hint"] = f"Application logs: {LOG_FILE}"
        # Record what the run will actually do, before it does it. Three
        # separate bugs looked identical from the UI — a run that finished with
        # none of your keywords searched — and the run state was the only place
        # that could have said why.
        state["parallel_workers"] = max(1, min(int(request.parallel_workers or 1), 3))
        state["run_mode"] = request.run_mode
        state["profile_is_active"] = profile.profile_id == (
            read_active() or {}
        ).get("profile_id")

        from modules.orchestrator import collection as coll

        if request.run_mode == "generate" and coll.harvest_active(
            request.category or "career_jobs", profile.profile_id
        ):
            state["note"] = (
                "Harvest mode is active: the AI will re-search this profile's own "
                "keyword pool instead of generating new ones. Your keywords are "
                "searched first."
            )

        logger.info(
            "Run %s starting with %d keyword(s), mode=%s, profile=%s",
            run_id, len(filters.keywords), request.run_mode, profile.profile_id,
        )

        # Live progress. A run that scrapes for an hour before returning
        # anything looks identical to a hung one, so the run state carries what
        # is happening right now — keyword, ads harvested so far — and the UI
        # polls it. Without this there is no way to tell "working" from
        # "stuck" except by watching a browser tab.
        def on_progress(**event):
            phase = event.get("phase")
            if phase == "harvested":
                keyword = event.get("keyword")
                state["current_keyword"] = keyword
                state["harvested_total"] = int(event.get("harvested") or 0)
                state["last_harvest_at"] = datetime.now().isoformat()
                state["phase"] = "scraping"
                logger.info(
                    "📥 %r: %d ads harvested (%d unique this run)",
                    keyword, int(event.get("batch_size") or 0), int(event.get("harvested") or 0),
                )
                counts = state.setdefault(
                    "counts", {"accepted": 0, "rejected": 0, "ai_failed": 0}
                )
                total = max(1, state.get("total_keywords") or 1)
                done = len(state.get("keywords_harvested") or [])
                state.setdefault("keywords_harvested", [])
                if keyword and keyword not in state["keywords_harvested"]:
                    state["keywords_harvested"].append(keyword)
                    done = len(state["keywords_harvested"])
                # 5% floor, 90% ceiling: the AI judging tail owns the last slice.
                state["progress"] = max(5, min(90, int(5 + (done / total) * 85)))
            elif phase == "judging":
                state["phase"] = "judging"
                state["current_keyword"] = event.get("keyword")

        summary = await _run_with_filters(
            filters,
            None if request.run_until_complete else (request.cycles or 1),
            request.run_mode,
            run_id,
            request.profile_id,
            request.request_id,
            max(1, min(int(request.parallel_workers or 1), 3)),
            on_progress=on_progress,
        )

        counts = state.setdefault("counts", {"accepted": 0, "rejected": 0, "ai_failed": 0})
        counts["accepted"] = int(summary.get("relevant_ads", 0))
        counts["rejected"] = int(summary.get("rejected_ads", 0) or 0)

        state.update(
            status="completed",
            progress=100,
            cycles=summary.get("cycles"),
            keywords_searched=summary.get("keywords_searched"),
            completed_at=datetime.now().isoformat(),
        )
        # The orchestrator's real list (provided keywords + AI batches, searched
        # once each) supersedes the request's keywords once the run is done.
        searched = summary.get("searched_keywords") or []
        if searched:
            state["keywords"] = list(searched)
        # Say plainly when a run did not search what was asked for. Silently
        # returning fewer keywords than were typed is the worst failure mode
        # here: the run reports success and the user has to guess why.
        requested = list(filters.keywords or [])
        missed = [k for k in requested if k not in set(searched)]
        if missed:
            state["keywords_not_searched"] = missed
            logger.warning(
                "Run %s did not search %d requested keyword(s): %s",
                run_id, len(missed), missed,
            )
    except Exception as exc:  # noqa: BLE001 — surfaced to the UI via status
        logger.exception("Run %s failed", run_id)
        run_states[run_id].update(status="failed", error=str(exc), completed_at=datetime.now().isoformat())


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.post("/api/runs")
async def start_run(request: RunRequest, background_tasks: BackgroundTasks):
    """Start a new orchestrator run and return immediately."""
    if not request.keywords and request.run_mode == "existing":
        raise HTTPException(400, "Add at least one keyword, or switch to AI keyword generation.")

    run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    run_states[run_id] = {
        "id": run_id,
        "status": "starting",
        "progress": 0,
        "current_keyword": None,
        "keywords": request.keywords or [],
        "counts": {"accepted": 0, "rejected": 0, "ai_failed": 0},
        "started_at": datetime.now().isoformat(),
        "config": request.model_dump(),
    }

    background_tasks.add_task(run_orchestrator_background, run_id, request)

    return {"id": run_id, "run_id": run_id, "status": "started"}


@app.get("/api/runs")
async def list_runs(limit: int = 50):
    """List runs, combining in-flight runs with persisted result files."""
    runs: list[dict[str, Any]] = []

    for cat_dir in (DATA_DIR.iterdir() if DATA_DIR.exists() else []):
        if not cat_dir.is_dir():
            continue
        for run_file in sorted(cat_dir.glob("run_*.json"), reverse=True)[:limit]:
            try:
                data = json.loads(run_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            runs.append(
                {
                    "id": run_file.stem,
                    "category": cat_dir.name,
                    "status": "completed",
                    "started_at": data.get("recorded_at", ""),
                    "counts": {
                        "accepted": len(data.get("accepted", []) or []),
                        "rejected": len(data.get("judged_rejected", []) or []),
                        "ai_failed": len(data.get("ai_failed", []) or []),
                    },
                }
            )

    for run_id, state in run_states.items():
        if state.get("status") not in TERMINAL_STATES:
            runs.append(
                {
                    "id": run_id,
                    "category": state.get("config", {}).get("category", "career_jobs"),
                    "status": state.get("status"),
                    "started_at": state.get("started_at", ""),
                    "progress": state.get("progress", 0),
                    "counts": state.get("counts", {"accepted": 0, "rejected": 0, "ai_failed": 0}),
                }
            )

    runs.sort(key=lambda run: run.get("started_at") or "", reverse=True)
    return {"runs": runs[:limit]}


@app.get("/api/runs/{run_id}")
async def get_run(run_id: str):
    """Full run detail (live state takes precedence over the saved file)."""
    if run_id in run_states:
        return run_states[run_id]

    run_file = _find_run_file(run_id)
    if run_file is None:
        raise HTTPException(404, "Run not found")

    data = json.loads(run_file.read_text(encoding="utf-8"))
    return {
        "id": run_id,
        "category": run_file.parent.name,
        "status": "completed",
        "progress": 100,
        "data": data,
        "counts": {
            "accepted": len(data.get("accepted", []) or []),
            "rejected": len(data.get("judged_rejected", []) or []),
            "ai_failed": len(data.get("ai_failed", []) or []),
        },
        "started_at": data.get("recorded_at", ""),
    }


@app.get("/api/runs/{run_id}/status")
async def get_run_status(run_id: str):
    """Small payload used for 3-second polling."""
    if run_id in run_states:
        state = run_states[run_id]
        return {
            "id": run_id,
            "status": state.get("status"),
            "progress": state.get("progress", 0),
            "current_keyword": state.get("current_keyword"),
            "counts": state.get("counts", {}),
            "error": state.get("error"),
        }

    run_file = _find_run_file(run_id)
    if run_file is None:
        raise HTTPException(404, "Run not found")

    return {"id": run_id, "status": "completed", "progress": 100, "counts": {}}


@app.get("/api/config")
async def get_config():
    """Profile text plus the last saved filter selection."""
    from interface.select_filters import _load_last_selection
    from modules.storage import load_profile

    about_us, additional_filters = load_profile()
    return {
        "about_us": about_us,
        "additional_filters": additional_filters,
        "last_selection": _load_last_selection(),
    }


@app.post("/api/config")
async def save_config(config: ConfigRequest):
    """Persist the About You / What Are You Looking For answers."""
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)

    if config.about_us is not None:
        (PROFILE_DIR / "about_us.txt").write_text(config.about_us, encoding="utf-8")
    if config.additional_filters is not None:
        (PROFILE_DIR / "additional_filters.txt").write_text(config.additional_filters, encoding="utf-8")

    return {"status": "saved"}


def find_free_port(start_port: int = 8000, max_port: int = 9000) -> int:
    """Return the first free TCP port in [start_port, max_port]."""
    for port in range(start_port, max_port + 1):
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
                probe.bind(("", port))
                return port
        except OSError:
            continue
    raise RuntimeError(f"No free ports in range {start_port}-{max_port}")


def configure_logging() -> None:
    """
    Send application logs to stdout AND to a file, once, whatever started us.

    This used to live under `if __name__ == "__main__"`, so `uvicorn
    interface.main:app` produced no application output at all — only uvicorn's
    access log. It was then moved to import time and to stdout alone, which
    still left logs invisible in two real situations: output piped to another
    process (block-buffered, so lines appear in 4KB lumps or never), and a
    backend started from an IDE or a GUI launcher whose console is not
    attached to the process that serves the browser.

    The file removes both problems, because it does not care how the process
    was started or where its stdout went. stdout stays for the terminal case,
    where watching the crawl live is genuinely the better experience.

    Rotation is bounded: this writes per scroll step and per verdict, and an
    unbounded log in a repo is a liability rather than a diagnostic.
    """
    root = logging.getLogger()
    if any(getattr(h, "_tesserae", False) for h in root.handlers):
        return

    fmt = logging.Formatter(
        "%(asctime)s | %(levelname)-7s | %(name)-28s | %(message)s"
    )

    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(fmt)
    console._tesserae = True  # type: ignore[attr-defined]
    root.addHandler(console)

    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        rotating = RotatingFileHandler(
            LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
        rotating.setFormatter(fmt)
        rotating._tesserae = True  # type: ignore[attr-defined]
        root.addHandler(rotating)
    except OSError as exc:  # a read-only checkout must still be able to run
        console.handle(
            logging.LogRecord(
                "tesserae", logging.WARNING, __file__, 0,
                f"Could not open {LOG_FILE}: {exc} — logging to terminal only",
                None, None,
            )
        )

    root.setLevel(os.getenv("TESSERAE_LOG_LEVEL", "INFO").upper())


configure_logging()


if __name__ == "__main__":
    import uvicorn

    port = find_free_port(8000)
    print(f"Tesserae API listening on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
