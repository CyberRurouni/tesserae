"""
Manual judging workflow.

    python3 -m interface.manual scrape
    python3 -m interface.manual apply <run_id>

`scrape` runs the existing scraper and heuristics, writes the surviving ads to
data/agent_audits/pending_<run_id>.json, and leaves judging to the assistant.
`apply` turns a filled verdicts file into the normal run file and the persistent
verdict sets the rest of Tesserae already uses.
"""

import argparse
import asyncio
import json
import logging
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from core import AdRecord, AdRelevanceVerdict
from genesis.config import ADS_DIR, DATA_DIR
from interface.select_filters import (
    _load_last_selection,
    _parse_keywords_input,
    build_scraping_filters,
)
from modules.profiles import deferred as deferred_store
from modules.profiles import held as held_store
from modules.profiles.active import ensure_active_profile
from modules.scraper import run_scrape
from modules.scraper.seen import RunSeenIds
from modules.storage import manual_progress
from modules.storage.profile import resolve_lens_state
from modules.storage.results import record_results
from modules.storage.run_results import begin_run, write_run_file
from modules import verdicts as verdict_store

logger = logging.getLogger(__name__)

AUDITS_DIR = DATA_DIR / "agent_audits"
BUCKETS = ("accepted", "deferred", "judged_rejected")
BUCKET_ALIASES = {
    "rejected": "judged_rejected",
    "reject": "judged_rejected",
    "defer": "deferred",
    "accept": "accepted",
}
STAGES = ("profile", "request")
REJECTION_KINDS = ("no_hiring", "not_for_me")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _new_run_id() -> str:
    return f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"


def pending_path(run_id: str) -> Path:
    return AUDITS_DIR / f"pending_{run_id}.json"


def verdicts_path(run_id: str) -> Path:
    return AUDITS_DIR / f"verdicts_{run_id}.json"


def _write_json(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def _build_filters(args) -> object:
    last = _load_last_selection() or {}
    keywords = _parse_keywords_input(args.keywords) if args.keywords else last.get("keywords")
    if not keywords:
        raise SystemExit(
            "No keywords selected. Pass --keywords or save a search in the UI first."
        )

    max_ads = args.max_ads_per_keyword
    if max_ads is None:
        max_ads = last.get("max_ads_per_keyword")

    return build_scraping_filters(
        run_mode="existing",
        non_interactive=True,
        category=args.category or last.get("category"),
        delivery_status=last.get("delivery_status"),
        platforms=last.get("platforms"),
        media_type=last.get("media_type"),
        custom_start_date=args.date_from or last.get("custom_start_date"),
        custom_end_date=args.date_to or last.get("custom_end_date"),
        keywords=keywords,
        ad_age=args.ad_age if args.ad_age is not None else last.get("ad_age_hours"),
        regions=last.get("delivered_to_regions"),
        languages=last.get("languages"),
        sort_order=last.get("sort_order"),
        heuristic_patterns=last.get("heuristic_patterns"),
        max_ads_per_keyword=max_ads,
    )


async def _scrape(args) -> int:
    await ensure_active_profile()
    lens = resolve_lens_state(args.profile_id, args.request_id)
    filters = _build_filters(args)
    run_id = args.run_id or _new_run_id()

    AUDITS_DIR.mkdir(parents=True, exist_ok=True)
    pending = pending_path(run_id)
    verdicts = verdicts_path(run_id)
    stream = pending.with_suffix(".jsonl")
    if pending.exists() or verdicts.exists() or stream.exists():
        raise SystemExit(f"Run {run_id} already has pending files in {AUDITS_DIR}")

    ads: dict[str, AdRecord] = {}
    handle = stream.open("w", encoding="utf-8")

    started_at = _now()
    keywords_harvested: list[str] = []
    sieve_totals = {"cards": 0, "already_decided": 0, "kept": 0, "to_ai": 0}
    progress_state = {
        "status": "running",
        "category": filters.category,
        "profile_id": lens.profile_id,
        "request_id": lens.request_id,
        "thresholds": {
            "profile": lens.profile_threshold,
            "request": lens.request_threshold,
        },
        "keywords": list(filters.keywords),
        "total_keywords": len(filters.keywords),
        "keywords_harvested": keywords_harvested,
        "started_at": started_at,
        "progress": 5,
        "phase": "starting",
        "current_keyword": None,
        "ads_collected": 0,
        "harvested_total": 0,
        "counts": {"accepted": 0, "held": 0, "rejected": 0, "ai_failed": 0},
        "log_hint": f"Pending file: {pending}",
    }

    def publish(**updates):
        progress_state.update(updates)
        try:
            manual_progress.write(run_id, progress_state)
        except Exception as exc:  # noqa: BLE001 - progress must not stop the scrape
            logger.warning("Could not publish manual progress: %s", exc)

    def publish_progress():
        total = max(1, len(filters.keywords))
        done = len(keywords_harvested)
        progress_state["progress"] = max(5, min(90, int(5 + (done / total) * 85)))
        publish()

    async def on_harvested(batch, keyword, total_unique):
        for ad in batch:
            if ad.ad_archive_id in ads:
                continue
            ads[ad.ad_archive_id] = ad
            handle.write(json.dumps(ad.model_dump(mode="json"), ensure_ascii=False) + "\n")
            handle.flush()

        if keyword and keyword not in keywords_harvested:
            keywords_harvested.append(keyword)
        progress_state.update(
            current_keyword=keyword,
            harvested_total=total_unique,
            ads_collected=len(ads),
            keywords_harvested=list(keywords_harvested),
        )
        publish_progress()

    async def on_step(**event):
        keyword = event.get("keyword")
        if keyword:
            progress_state["current_keyword"] = keyword
        progress_state["phase"] = "scraping"
        for source, target in (
            ("cards_seen", "cards_seen"),
            ("target", "cards_target"),
            ("ads_collected", "ads_collected"),
            ("estimate", "estimate"),
            ("steps", "scroll_steps"),
        ):
            if source in event:
                progress_state[target] = event.get(source)
        sieve = event.get("sieve")
        if sieve:
            for field in sieve_totals:
                sieve_totals[field] += int(sieve.get(field) or 0)
            progress_state["sieve_totals"] = dict(sieve_totals)
        publish_progress()

    stop = {"requested": False}

    def request_stop(signum, frame):
        stop["requested"] = True
        print("\nStopping after the current scroll; collected ads will be kept.", flush=True)

    previous_handler = signal.getsignal(signal.SIGINT)
    signal.signal(signal.SIGINT, request_stop)

    print(f"Run:      {run_id}")
    print(f"Profile:  {lens.profile_id or '(none)'}")
    print(f"Request:  {lens.request_id or '(none)'}")
    print(f"Keywords: {', '.join(filters.keywords)}")
    print(f"Category: {filters.category}")
    print("Judging:  manual (heuristics only; no Gemini verdicts)")
    publish()

    try:
        remaining = await run_scrape(
            filters,
            headless=args.headless,
            run_seen_ids=RunSeenIds(run_id=run_id),
            profile_id=lens.profile_id,
            request_id=lens.request_id,
            parallel_workers=args.parallel_workers,
            on_harvested=on_harvested,
            on_step=on_step,
            should_stop=lambda: stop["requested"],
            profile_threshold=lens.profile_threshold,
            request_threshold=lens.request_threshold,
        )
        for ad in remaining:
            if ad.ad_archive_id not in ads:
                ads[ad.ad_archive_id] = ad
                handle.write(json.dumps(ad.model_dump(mode="json"), ensure_ascii=False) + "\n")
    except Exception as exc:
        publish(status="failed", phase="failed", error=str(exc), completed_at=_now())
        raise
    finally:
        handle.close()
        signal.signal(signal.SIGINT, previous_handler)

    ad_list = list(ads.values())
    payload = {
        "run_id": run_id,
        "status": "pending",
        "category": filters.category,
        "profile_id": lens.profile_id,
        "request_id": lens.request_id,
        "profile_text": lens.profile_text,
        "request_text": lens.request_text,
        "thresholds": {
            "profile": lens.profile_threshold,
            "request": lens.request_threshold,
        },
        "keywords": list(filters.keywords),
        "scraped_at": _now(),
        "counts": {"ads": len(ad_list)},
        "ads": [ad.model_dump(mode="json") for ad in ad_list],
    }
    _write_json(pending, payload)

    template = {
        "run_id": run_id,
        "profile_id": lens.profile_id,
        "request_id": lens.request_id,
        "judged_at": "",
        "verdicts": [
            {
                "id": ad.ad_archive_id,
                "bucket": "",
                "stage": "",
                "reason": "",
                "rejection_kind": "not_for_me",
                "confidence": 1.0,
                "ad_category": "",
            }
            for ad in ad_list
        ],
    }
    _write_json(verdicts, template)

    publish(
        status="awaiting_verdicts",
        phase="awaiting_verdicts",
        progress=95,
        current_keyword=None,
        ads_collected=len(ad_list),
        harvested_total=max(progress_state.get("harvested_total") or 0, len(ad_list)),
        keywords_harvested=list(keywords_harvested),
        scraped_at=payload["scraped_at"],
        pending_file=str(pending),
        verdicts_file=str(verdicts),
    )

    print(f"\nPending ads: {len(ad_list)}")
    print(f"Pending file: {pending}")
    print(f"Verdicts file: {verdicts}")
    print(f"Stream file: {stream}")
    if not ad_list:
        print("No ads survived the heuristics.")
    return 0


def _load_apply_inputs(args) -> tuple[Path, Path, dict, dict]:
    if args.run_id:
        pending = Path(args.pending) if args.pending else pending_path(args.run_id)
        verdicts = Path(args.verdicts) if args.verdicts else verdicts_path(args.run_id)
    else:
        if not args.pending or not args.verdicts:
            raise SystemExit("Pass a run id, or both --pending and --verdicts.")
        pending = Path(args.pending)
        verdicts = Path(args.verdicts)

    if not pending.exists():
        raise SystemExit(f"Pending file not found: {pending}")
    if not verdicts.exists():
        raise SystemExit(f"Verdicts file not found: {verdicts}")
    return pending, verdicts, _read_json(pending), _read_json(verdicts)


def _build_verdicts(pending: dict, document: dict) -> tuple[list[AdRecord], dict[str, AdRelevanceVerdict]]:
    ads = [AdRecord(**raw) for raw in pending.get("ads") or []]
    by_id = {ad.ad_archive_id: ad for ad in ads}
    request_id = pending.get("request_id")
    rows = document.get("verdicts") or []
    verdicts: dict[str, AdRelevanceVerdict] = {}
    errors: list[str] = []

    for row in rows:
        ad_id = str(row.get("id") or "").strip()
        bucket = str(row.get("bucket") or "").strip().lower()
        bucket = BUCKET_ALIASES.get(bucket, bucket)

        if not ad_id:
            errors.append("a verdict row has no id")
            continue
        if ad_id not in by_id:
            errors.append(f"{ad_id}: not in the pending file")
            continue
        if ad_id in verdicts:
            errors.append(f"{ad_id}: duplicate verdict")
            continue
        if bucket not in BUCKETS:
            errors.append(f"{ad_id}: bucket must be one of {', '.join(BUCKETS)}")
            continue

        reason = str(row.get("reason") or "").strip()
        if not reason:
            errors.append(f"{ad_id}: reason is required")
            continue

        stage = str(row.get("stage") or "").strip().lower()
        if not stage:
            stage = "profile" if bucket == "judged_rejected" else "request"
        if stage not in STAGES:
            errors.append(f"{ad_id}: stage must be profile or request")
            continue

        rejection_kind = str(row.get("rejection_kind") or "not_for_me").strip().lower()
        if rejection_kind not in REJECTION_KINDS:
            errors.append(f"{ad_id}: rejection_kind must be no_hiring or not_for_me")
            continue

        try:
            confidence = float(row.get("confidence", 1.0))
        except (TypeError, ValueError):
            errors.append(f"{ad_id}: confidence must be a number")
            continue
        confidence = min(max(confidence, 0.0), 1.0)

        ad_category = str(row.get("ad_category") or "").strip()
        if not ad_category:
            ad_category = str(by_id[ad_id].model_dump(mode="json").get("ad_category") or "")

        verdicts[ad_id] = AdRelevanceVerdict(
            ad_archive_id=ad_id,
            outcome="rejected" if bucket == "judged_rejected" else bucket,
            rejection_kind=rejection_kind,
            confidence=confidence,
            reason=reason,
            ad_category=ad_category,
            request_id=request_id,
            stage=stage,
        )

    missing = sorted(set(by_id) - set(verdicts))
    extra = sorted(set(verdicts) - set(by_id))
    if missing:
        errors.append(f"{len(missing)} ad(s) have no verdict: {', '.join(missing[:10])}")
    if extra:
        errors.append(f"{len(extra)} verdict(s) have no ad: {', '.join(extra[:10])}")
    if errors:
        raise SystemExit("Verdicts file is not ready:\n  " + "\n  ".join(errors))

    return ads, verdicts


def _apply(args) -> int:
    pending_file, verdicts_file, pending, document = _load_apply_inputs(args)
    ads, verdicts = _build_verdicts(pending, document)

    run_id = str(pending.get("run_id") or document.get("run_id") or "").strip()
    category = str(pending.get("category") or "").strip()
    profile_id = pending.get("profile_id")
    request_id = pending.get("request_id")
    thresholds = pending.get("thresholds") or {}
    if not run_id or not category:
        raise SystemExit("Pending file is missing run_id or category.")

    ids = list(verdicts)
    verdict_store.forget_accepted(category, ids)
    verdict_store.forget_junk(category, ids)
    if profile_id:
        verdict_store.forget_rejected(profile_id, ids)
        deferred_store.resolve_many(profile_id, ids)
        held_store.resolve_many(profile_id, ids)

    begin_run(run_id, category, thresholds)
    summary = record_results(
        ads,
        verdicts,
        category,
        None,
        run_id=run_id,
        searched_keywords=pending.get("keywords"),
        profile_id=profile_id,
        request_id=request_id,
        profile_threshold=float(thresholds.get("profile", 0.5)),
        request_threshold=float(thresholds.get("request", 0.5)),
    )

    run_file = ADS_DIR / category / f"{run_id}.json"
    run_file.unlink(missing_ok=True)
    path = write_run_file(run_id, category=category, status="completed")
    if not path:
        raise SystemExit("No run file was written; the verdicts produced no rows.")

    payload = _read_json(Path(path))
    payload["recorded_at"] = pending.get("scraped_at") or payload.get("recorded_at")
    payload["judged_by"] = "assistant"
    payload["manual_judging"] = {
        "pending_file": str(pending_file),
        "verdicts_file": str(verdicts_file),
        "applied_at": _now(),
    }
    _write_json(Path(path), payload)

    applied_at = _now()
    pending["status"] = "applied"
    pending["applied_at"] = applied_at
    pending["run_file"] = path
    _write_json(pending_file, pending)
    document["applied_at"] = applied_at
    document["run_file"] = path
    _write_json(verdicts_file, document)
    manual_progress.clear(run_id)

    print(f"Run file: {path}")
    print(
        "Applied: "
        f"{len(summary['accepted'])} accepted, "
        f"{len(summary['deferred'])} deferred, "
        f"{len(summary['judged_rejects'])} rejected"
    )
    return 0


def _scrape_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Scrape with heuristics only, for manual judging.")
    parser.add_argument("--keywords", type=str, default=None,
                        help="Keywords (JSON array or space-separated). Defaults to the last saved search.")
    parser.add_argument("--category", type=str, default=None)
    parser.add_argument("--date-from", type=str, default=None)
    parser.add_argument("--date-to", type=str, default=None)
    parser.add_argument("--ad-age", type=int, default=None)
    parser.add_argument("--max-ads-per-keyword", type=int, default=None)
    parser.add_argument("--profile-id", type=str, default=None)
    parser.add_argument("--request-id", type=str, default=None)
    parser.add_argument("--parallel-workers", type=int, default=1, choices=[1, 2, 3])
    parser.add_argument("--run-id", type=str, default=None)
    parser.add_argument("--headless", action="store_true")
    return parser


def _apply_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Apply assistant verdicts to a manual run.")
    parser.add_argument("run_id", nargs="?", default=None)
    parser.add_argument("--pending", type=str, default=None)
    parser.add_argument("--verdicts", type=str, default=None)
    return parser


def main(argv: list | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = argparse.ArgumentParser(description="Manual scrape and verdict workflow.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("scrape", parents=[_scrape_parser()], add_help=False)
    sub.add_parser("apply", parents=[_apply_parser()], add_help=False)
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
    )

    if args.command == "scrape":
        return asyncio.run(_scrape(args))
    return _apply(args)


if __name__ == "__main__":
    raise SystemExit(main())
