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
import signal
import socket
import subprocess
import sys
import time
from datetime import datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any, Optional, Union

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, field_validator

from genesis.config import ADS_DIR

from interface import log_buffer
from interface.profiles_api import router as profiles_router
from interface.heuristics_api import router as heuristics_router
from interface.keywords_api import router as keywords_router
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

# Where run result files live. Read from genesis rather than recomputed from the
# repo root: two definitions of the same directory is how a run ends up written
# in one place and looked for in another, which is exactly what stopped a
# cancelled run from recording its own outcome.
DATA_DIR = ADS_DIR

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

TERMINAL_STATES = {"completed", "failed", "cancelled", "abandoned", "partial"}

# How long a run may go without any sign of life before it is assumed dead and
# the pipeline is handed to whoever asks next.
#
# Judging happens once per keyword, so before the per-scroll tick existed a
# single keyword on a big list could legitimately run for ten minutes without a
# single progress event — which would make any threshold here a guess. The run
# now reports on every scroll step, so this is measured against real motion.
STALE_RUN_SECONDS = 600


def _beat(run_id: str) -> datetime:
    """
    When this run last did anything, as a datetime. Never raises.

    These are stored as ISO strings (they are echoed to the browser), so they
    are parsed back here rather than kept in a second form that can drift out
    of step with what the UI shows.
    """
    state = run_states.get(run_id) or {}
    raw = state.get("last_progress_at") or state.get("started_at")
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            pass
    return datetime.now()


def live_run() -> tuple[str, dict] | None:
    """
    The run that currently owns the pipeline, if any.

    A run that has stopped reporting is declared `abandoned` and released, so a
    crash mid-crawl cannot lock the app out of searching forever. This is the
    only gate on concurrency: two runs would fight over the one browser profile
    directory (Chromium locks it), double the captcha exposure the single
    session exists to avoid, and interleave their logs into nonsense.
    """
    now = datetime.now()
    for run_id, state in list(run_states.items()):
        if state.get("status") in TERMINAL_STATES:
            continue
        idle = (now - _beat(run_id)).total_seconds()
        if idle > STALE_RUN_SECONDS:
            state["status"] = "abandoned"
            state["completed_at"] = now.isoformat()
            state["error"] = (
                f"No progress for {int(idle // 60)} minutes — the run is not "
                "believed to still be running, so it was stopped and the "
                "pipeline released."
            )
            # Stop it, don't just rename it.
            #
            # Marking the state alone left the crawl running: nothing held a
            # handle to the task, and the only stop signal in the system is this
            # flag. So an "abandoned" run went on crawling Meta while the UI
            # reported it dead, a second run was free to start beside it on the
            # one browser profile, and when it finally finished it overwrote its
            # own status with "completed" — a run reported dead turning into a
            # run reported successful, with its results mixed in.
            #
            # Setting the flag routes it through the same cooperative stop the
            # Stop button uses: it breaks at the next scroll step, keeps every ad
            # it collected, judges those, saves them, and closes the browser
            # cleanly. `stop_reason` is what lets the end of the run report this
            # as its own outcome instead of borrowing "cancelled".
            state["cancel_requested"] = True
            state["stop_reason"] = "idle_timeout"
            logger.warning(
                "⛔ Run %s idle for %d minute(s) — stopping it and releasing the "
                "pipeline. Whatever it collected is kept and judged.",
                run_id, idle // 60,
            )
            continue
        return run_id, state
    return None


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
    # Harvest (all-covered mode): how many of the pool's best keywords to
    # re-search, or an explicit list that overrides the number. None leaves the
    # decision to the backend's ranking.
    harvest_limit: int = 20
    harvest_keywords: Optional[list[str]] = None

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
    """Only the last-used filter selection is saved here.

    Profile text used to be writable through this endpoint, but it now lives in
    the profile store and is edited through /api/profiles — one writer, one copy.
    The fields stayed long after the screens stopped sending them.
    """

    last_selection: Optional[dict] = None


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
app.include_router(keywords_router)


# ── Run lifecycle ──────────────────────────────────────────────────────────

async def run_orchestrator_background(run_id: str, request: RunRequest) -> None:
    """Execute the orchestrator, updating the polled run state as it goes."""
    state = run_states.setdefault(run_id, {"id": run_id})
    try:
        started = datetime.now()
        state.update(
            status="running",
            started_at=state.get("started_at") or started.isoformat(),
            last_progress_at=started.isoformat(),
            # Defaulted, never reset. This used to be a hard `False`, which threw
            # away a stop that arrived in the window between the run being
            # accepted and this task starting — the user pressed Stop, the flag
            # was set, and the run began anyway and could not be stopped at all.
            cancel_requested=bool(state.get("cancel_requested")),
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
            from modules.orchestrator import collection as kcoll

            selection, notes = kcoll.resolve_harvest(
                kcoll.load_collection(request.category or "career_jobs",
                                      profile.profile_id)["searchable"],
                max(0, int(request.harvest_limit or 0)),
                request.harvest_keywords or None,
            )
            state["harvest_keywords"] = selection
            state["note"] = (
                "Harvest mode is active: instead of generating new keywords, "
                f"Tesserae re-searches {len(selection)} of this profile's "
                "highest-yielding keywords for ads posted since. Your own "
                "keywords are searched first."
            )
            for note in notes:
                state.setdefault("harvest_notes", []).append(note)
                logger.warning("Harvest selection: %s", note)

        logger.info(
            "Run %s starting with %d keyword(s), mode=%s, profile=%s",
            run_id, len(filters.keywords), request.run_mode, profile.profile_id,
        )

        # Live progress. A run that scrapes for an hour before returning
        # anything looks identical to a hung one, so the run state carries what
        # is happening right now — keyword, ads harvested so far — and the UI
        # polls it. Without this there is no way to tell "working" from
        # "stuck" except by watching a browser tab.
        def heartbeat():
            """Any sign of life at all refreshes the stale-run clock."""
            state["last_progress_at"] = datetime.now().isoformat()

        def on_step(**event):
            # Per scroll step, from inside the crawl. This is what makes a long
            # keyword legible (cards seen against Meta's estimate) and what
            # stops the stale-run threshold from being a guess.
            heartbeat()
            state["current_keyword"] = event.get("keyword")
            state["phase"] = "scraping"
            state["cards_seen"] = event.get("cards_seen")
            state["cards_target"] = event.get("target")
            state["ads_collected"] = event.get("ads_collected")
            state["estimate"] = event.get("estimate")
            state["scroll_steps"] = event.get("steps")

        def on_progress(**event):
            heartbeat()
            phase = event.get("phase")
            if phase == "judging":
                # Judging used to be the one long silent stretch, which made a
                # working run look like a hung one. Say what it is doing, and let
                # the heartbeat above keep the idle detector honest.
                keyword = event.get("keyword")
                if keyword:
                    state["current_keyword"] = keyword
                judged = int(event.get("judged") or 0)
                judging_total = int(event.get("judging_total") or 0)
                state["phase"] = "judging"
                state["ads_judged"] = judged
                state["ads_judging_total"] = judging_total
                logger.info(
                    "⚖️  %r: judged %d/%d ad(s)", keyword or "-", judged, judging_total,
                )
                return
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
            harvest_limit=max(0, int(request.harvest_limit or 0)),
            harvest_keywords=request.harvest_keywords or None,
            on_progress=on_progress,
            on_step=on_step,
            # Read by the run itself, not by this function: a cooperative stop
            # that the crawl polls on its next scroll step, rather than a task
            # cancelled underneath it mid-await.
            should_stop=lambda: bool(run_states.get(run_id, {}).get("cancel_requested")),
        )

        counts = state.setdefault("counts", {"accepted": 0, "rejected": 0, "ai_failed": 0})
        counts["accepted"] = int(summary.get("relevant_ads", 0))
        counts["rejected"] = int(summary.get("rejected_ads", 0) or 0)

        stopped = bool(state.get("cancel_requested"))
        # Why it stopped, in the run's own words. "We gave up waiting" and "the
        # user pressed Stop" both stop a run, but only one of them is a fault,
        # and a status that cannot tell them apart is worse than no status: it
        # teaches you to read a healthy finish as a problem.
        stop_reason = state.get("stop_reason")
        if stop_reason == "idle_timeout":
            final_status = "partial"
            state["error"] = (
                f"Stopped after {STALE_RUN_SECONDS // 60} minutes with no progress. "
                "Everything collected before that point was judged and saved — "
                "this is a partial result, not a failed one."
            )
        elif stopped:
            final_status = "cancelled"
        else:
            final_status = "completed"
        state.update(
            status=final_status,
            stop_reason=stop_reason,
            progress=100 if final_status != "partial" else state.get("progress", 0),
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
        # Write the outcome into the run file so history is honest after a
        # restart — otherwise a stopped run reads as "completed" forever.
        _finalise_run_file(run_id, state.get("status", "completed"))

    except Exception as exc:  # noqa: BLE001 — surfaced to the UI via status
        logger.exception("Run %s failed", run_id)
        run_states[run_id].update(status="failed", error=str(exc), completed_at=datetime.now().isoformat())
        _finalise_run_file(run_id, "failed")


# ── Routes ─────────────────────────────────────────────────────────────────

@app.get("/health")
async def health():
    return {"status": "ok", "timestamp": datetime.now().isoformat()}


@app.get("/api/logs")
async def recent_logs(limit: int = 200, logger: str | None = None):
    """
    The live log tail, for the run page.

    Read-only and deliberately dumb: it returns the most recent lines the
    process has emitted. Enough to answer "is it working, and on what" while a
    run is going, without opening a terminal or waiting for a log to flush.
    """
    return {
        "file": str(LOG_FILE),
        "lines": log_buffer.snapshot(limit=limit, name_filter=logger),
    }


@app.post("/api/runs")
async def start_run(request: RunRequest, background_tasks: BackgroundTasks):
    """Start a new orchestrator run and return immediately."""
    if not request.keywords and request.run_mode == "existing":
        raise HTTPException(400, "Add at least one keyword, or switch to AI keyword generation.")

    # One run at a time. Two would fight over the single browser profile
    # directory, double the captcha exposure, and interleave their log tails.
    busy = live_run()
    if busy:
        busy_id, busy_state = busy
        raise HTTPException(
            409,
            f"A search is already running ({busy_id}) — "
            f"{busy_state.get('current_keyword') or 'starting up'}. "
            f"Stop it before starting another: /api/runs/{busy_id}/cancel",
        )

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


def _finalise_run_file(run_id: str, status: str) -> None:
    """
    Stamp a run's real outcome into its result file.

    A run that was stopped at 40% must not read as "completed" months later just
    because that is the only word the file knew. No file is created here: a run
    that found nothing has nothing to record.
    """
    run_file = _find_run_file(run_id)
    if run_file is None:
        return
    try:
        payload = json.loads(run_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return
    payload["status"] = status
    payload["completed_at"] = datetime.now().isoformat()
    run_file.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")


@app.post("/api/runs/{run_id}/cancel")
async def cancel_run(run_id: str):
    """
    Ask a running search to stop.

    Cooperative, not forceful: the run notices on its next scroll step (~3s),
    unwinds cleanly, and everything it has already collected is judged and
    saved. Nothing is lost except the cards it had not scrolled to yet.
    """
    state = run_states.get(run_id)
    if state is None:
        raise HTTPException(404, "Run not found")

    status = state.get("status")
    if status in TERMINAL_STATES:
        return {"run_id": run_id, "status": status,
                "note": "This run had already finished."}

    state["cancel_requested"] = True
    state["status"] = "cancelling"
    logger.info("🛑 Stop requested for run %s", run_id)
    return {"run_id": run_id, "status": "cancelling"}


@app.get("/api/runs/active")
async def get_active_run():
    """Which run owns the pipeline right now, if any. Drives the Start button."""
    busy = live_run()
    if not busy:
        return {"active": None}
    run_id, state = busy
    return {"active": {"run_id": run_id, "status": state.get("status"),
                       "current_keyword": state.get("current_keyword")}}


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
                    # A run stopped part-way says so, rather than reading as a
                    # finished one after a restart.
                    "status": data.get("status") or "completed",
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
        "status": data.get("status") or "completed",
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
    """The active profile's texts plus the last saved filter selection."""
    from interface.select_filters import _load_last_selection
    from modules.storage import resolve_lens

    profile_text, request_text = resolve_lens()
    return {
        "profile_text": profile_text,
        "request_text": request_text,
        "last_selection": _load_last_selection(),
    }


@app.post("/api/config")
async def save_config(config: ConfigRequest):
    """Persist the last used filter selection. Profile text lives in /api/profiles."""
    if config.last_selection is not None:
        from interface.select_filters import _save_last_selection

        _save_last_selection(config.last_selection)

    return {"status": "saved"}


def _listeners_on(port: int) -> list[tuple[int, str]]:
    """(pid, command) for every process listening on `port`. Empty if none."""
    try:
        out = subprocess.run(
            ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN", "-F", "pcn"],
            capture_output=True, text=True, timeout=10,
        ).stdout
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("⚠️ Could not inspect port %d (%s)", port, exc)
        return []
    found: list[tuple[int, str]] = []
    pid = None
    for line in out.splitlines():
        if line.startswith("p"):
            try:
                pid = int(line[1:])
            except ValueError:
                pid = None
        elif line.startswith("c") and pid is not None:
            found.append((pid, line[1:]))
    return found


def reclaim_port(port: int = 8000, wait_s: float = 10.0) -> int:
    """
    Take `port`, evicting whatever is squatting on it.

    This used to walk forward to the next free port instead, which was worse
    than useless: the frontend is pinned to 8000 (frontend/next.config.js), so a
    backend that quietly moved to 8001 left the browser talking to 8001's
    *absent* predecessor — the stale server from the previous run. The port
    conflict did not go away; it was hidden behind a working-looking page.

    Evicting is the honest repair, and it is also complete: the process holding
    8000 is the previous Tesserae, and killing it takes its Chromium with it
    (Playwright's browser dies with the driver), so the profile lock, the
    half-finished run and the crawl in flight all go at once. That was measured,
    not assumed — see the notes in PROBLEMS.md.

    Refuses to guess. If the holder cannot be identified, or survives the kill,
    this raises instead of moving ports, because a backend on an unexpected port
    is the exact failure being fixed here.
    """
    listeners = _listeners_on(port)
    if not listeners:
        return port

    for pid, command in listeners:
        if pid == os.getpid():
            continue
        logger.warning(
            "🔌 Port %d is held by pid %d (%s) — stopping it and taking the port",
            port, pid, command,
        )
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            continue
        except PermissionError as exc:
            raise RuntimeError(
                f"Port {port} is held by pid {pid} ({command}) and we may not "
                f"stop it: {exc}. Stop it yourself and relaunch."
            ) from exc

    deadline = time.monotonic() + wait_s
    while time.monotonic() < deadline:
        if not _listeners_on(port):
            logger.warning("🔌 Port %d reclaimed", port)
            return port
        time.sleep(0.25)

    for pid, command in _listeners_on(port):
        logger.warning("🔌 pid %d (%s) ignored SIGTERM — sending SIGKILL", pid, command)
        try:
            os.kill(pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
    time.sleep(0.5)
    if _listeners_on(port):
        raise RuntimeError(
            f"Port {port} is still held after evicting its owner. Refusing to "
            f"start on a different port — the frontend expects {port}."
        )
    logger.warning("🔌 Port %d reclaimed", port)
    return port


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

    # Live tail for the browser. The file below is the durable record; this is
    # what lets a run be watched while it happens, so it can be stopped on
    # evidence rather than on suspicion.
    log_buffer.install(fmt)

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

    port = reclaim_port(8000)
    print(f"Tesserae API listening on http://localhost:{port}")
    uvicorn.run(app, host="0.0.0.0", port=port)
