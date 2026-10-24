"""
run_results.py — the run's results, held live in Redis and written once at the end.

Why this exists
---------------
A run's result file is a single JSON document, and JSON cannot be appended to: to
add one ad you must rewrite everything already in it. So the file was being
rewritten after every judged group of 20. Measured, on a keyword returning 1,400
ads: to store 2 MB of results it wrote **95 MB**, because the 70th save
re-serialised 1,400 ads in order to add 20 more. Quadratic, and made 70x worse by
streaming the harvest.

It also meant the file was the only copy. A crash cost at most one group.

How this works
--------------
While the run is going, each judged ad is **appended to a Redis list owned by that
run**. RPUSH costs the same whether the list holds 20 ads or 20,000 — no rewriting,
so the quadratic disappears entirely. Redis is a separate process, so the data
survives the Python app dying, which is the crash that actually happens (and which
the port reclaimer now causes on purpose when a stale server is evicted).

When the run reaches any terminal state — finished, stopped, or partial — the list
is read back, assembled into the normal JSON file **once**, and the key deleted.
The key is deleted only after the write is confirmed; a failed write keeps it, so
a retryable disk error cannot become a lost run.

A run that dies before it can finish leaves its key behind. `sweep_orphaned_runs()`
writes those to disk as `partial` on the next start, and the TTL is the backstop
for the case where the app is never started again.

Everything here is best-effort by design: if Redis is unavailable the caller falls
back to whatever it holds in memory, so a Redis outage costs the crash insurance
rather than the run.
"""

import json
import logging
from datetime import datetime, timezone

from core import ADS_DIR
from core.utils.redis_utils import run_state_broker, safe_redis_operation

logger = logging.getLogger(__name__)

# One list per run. A live run refreshes its TTL on every append, so a long run is
# never reaped mid-flight; this only bounds how long a key from a DEAD run can
# sit there. Seven days is generous on purpose: it has to outlast "crashed on
# Friday, opened the app on Monday", and a crashed run's results are worthless if
# they evaporate before you come back for them.
RUN_RESULTS_TTL_SECONDS = 7 * 24 * 60 * 60

# Buckets, in the order they appear in the finished file. `_meta` is a header
# written once; `keyword` entries accumulate as keywords are searched.
_BUCKETS = ("accepted", "held", "deferred", "judged_rejected", "ai_failed")


def _key(run_id: str) -> str:
    return f"tesserae:run_results:{run_id}"


def _touch(run_id: str) -> None:
    safe_redis_operation(run_state_broker.expire, _key(run_id), RUN_RESULTS_TTL_SECONDS)


def begin_run(
    run_id: str,
    category: str,
    thresholds: dict[str, float] | None = None,
) -> None:
    """Open a run's list with its header. Cheap, and safe to call twice."""
    if not run_id:
        return
    header = json.dumps({
        "_meta": {
            "category": category,
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            "thresholds": thresholds or {"profile": 0.5, "request": 0.5},
        }
    })
    ok = safe_redis_operation(run_state_broker.rpush, _key(run_id), header)
    if ok is None:
        logger.warning(
            "⚠️ Could not open the live result store for %s — results will be kept "
            "in memory only and written when the run ends.", run_id,
        )
        return
    _touch(run_id)


def append_results(run_id: str, buckets: dict[str, list[dict]] | None = None,
                   searched_keywords: list[str] | None = None) -> int:
    """
    Append one judged group. Returns how many entries landed in Redis.

    Buckets is {bucket_name: [entry, ...]}. Only non-empty buckets are sent, so a
    group of 20 rejects costs 20 pushes, not 20 plus three empty ones.

    Returns 0 when there is nothing to store or Redis declined it — the caller
    still holds these in memory, so this is a loss of crash insurance, not of
    results.
    """
    if not run_id or not buckets:
        return 0

    payload: list[str] = []
    if searched_keywords:
        for kw in searched_keywords:
            if isinstance(kw, str) and kw:
                payload.append(json.dumps({"keyword": kw}, ensure_ascii=False))
    for name in _BUCKETS:
        for entry in buckets.get(name) or []:
            payload.append(json.dumps({"bucket": name, "entry": entry},
                                      ensure_ascii=False))
    if not payload:
        return 0

    written = safe_redis_operation(run_state_broker.rpush, _key(run_id), *payload)
    if written is None:
        logger.warning(
            "⚠️ Could not stage %d result(s) for %s in Redis — they are still in "
            "memory and will be written when the run ends.", len(payload), run_id,
        )
        return 0
    _touch(run_id)
    return len(payload)


def read_run(run_id: str) -> dict | None:
    """The run's assembled payload, or None if there is nothing staged."""
    if not run_id:
        return None
    raw = safe_redis_operation(run_state_broker.lrange, _key(run_id), 0, -1)
    if not raw:
        return None
    return _assemble(raw)


def _assemble(raw) -> dict:
    """Turn the flat list back into the finished file's shape."""
    payload: dict = {"accepted": [], "held": [], "deferred": [],
                     "judged_rejected": [], "ai_failed": [], "keywords": []}
    seen_keywords: set[str] = set()
    for item in raw:
        try:
            obj = json.loads(item)
        except (TypeError, json.JSONDecodeError):
            logger.warning("⚠️ Skipping an unreadable staged result entry")
            continue
        meta = obj.get("_meta")
        if meta:
            payload["category"] = meta.get("category") or payload.get("category")
            payload["recorded_at"] = meta.get("recorded_at") or payload.get("recorded_at")
            payload["thresholds"] = meta.get("thresholds") or payload.get("thresholds")
            continue
        kw = obj.get("keyword")
        if kw and kw not in seen_keywords:
            seen_keywords.add(kw)
            payload["keywords"].append(kw)
            continue
        bucket, entry = obj.get("bucket"), obj.get("entry")
        if bucket in payload and entry is not None:
            payload[bucket].append(entry)
    return payload


def discard(run_id: str) -> None:
    """Delete a run's staged results. Only ever called after a confirmed write."""
    if run_id:
        safe_redis_operation(run_state_broker.delete, _key(run_id))


def write_run_file(run_id: str, category: str | None = None,
                   status: str | None = None) -> str | None:
    """
    Assemble the staged results into the run's file, ONCE, and clear the key.

    Returns the path written, or None when there was nothing staged (a run that
    found no ads has nothing to record — that is not an error, and it is why no
    empty file is created).

    The key is deleted only after the write succeeds. If the write raises, the
    key survives so a retry — or `sweep_orphaned_runs` on the next start — still
    has the data. Deleting first and writing second would turn a disk error into a
    lost run, which is the exact failure this whole change exists to prevent.
    """
    payload = read_run(run_id)
    if not payload:
        return None
    cat = category or payload.pop("category", None)
    if not cat:
        logger.warning("⚠️ Run %s has staged results but no category — keeping them "
                       "in Redis rather than guessing a folder.", run_id)
        return None

    if status:
        payload["status"] = status
    if not any(payload.get(b) for b in _BUCKETS):
        logger.info("🗄️ Run %s staged no ads — nothing to write", run_id)
        discard(run_id)
        return None

    cat_dir = ADS_DIR / cat
    cat_dir.mkdir(parents=True, exist_ok=True)
    path = cat_dir / f"{run_id}.json"
    previous: dict = {}
    if path.exists():
        # Only reachable for a file left by an older version mid-run, or by a
        # retry after a failed write. Merging keeps those rows.
        try:
            previous = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, json.JSONDecodeError):
            logger.warning("⚠️ Unreadable run file %s — rewriting from staged results", path)
            previous = {}
    if previous:
        for bucket in _BUCKETS:
            merged = list(previous.get(bucket) or [])
            known = {e.get("ad_archive_id") for e in merged if isinstance(e, dict)}
            merged += [e for e in payload.get(bucket) or []
                       if not isinstance(e, dict) or e.get("ad_archive_id") not in known]
            payload[bucket] = merged
        payload["recorded_at"] = previous.get("recorded_at") or payload.get("recorded_at")
        payload["thresholds"] = payload.get("thresholds") or previous.get("thresholds")
        if not status and previous.get("status"):
            payload["status"] = previous["status"]
        kw = list(previous.get("keywords") or [])
        for k in payload.get("keywords") or []:
            if k not in kw:
                kw.append(k)
        payload["keywords"] = kw

    # A dict that never had a category in it must not keep the key either.
    payload.pop("category", None)
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    discard(run_id)
    logger.info(
        "🗄️ Run %s written: %d accepted, %d held, %d deferred, %d rejected, "
        "%d AI-failed",
        run_id, len(payload["accepted"]), len(payload["held"]),
        len(payload["deferred"]), len(payload["judged_rejected"]),
        len(payload["ai_failed"]),
    )
    return str(path)


def sweep_orphaned_runs() -> int:
    """
    Write out results left behind by runs whose process died.

    This is the ONLY path back from a real crash. The idle detector cannot help
    here: it runs inside the app, so a crashed app has nothing left running to
    notice anything. Whatever was staged in Redis is simply still there, and this
    is what turns it back into results the user can see.

    Each is stamped `partial`, because that is exactly what it is: the run did not
    finish, and everything it had found was kept and judged.

    Returns how many runs were recovered.
    """
    try:
        keys = run_state_broker.keys("tesserae:run_results:*") or []
    except Exception as exc:  # noqa: BLE001
        logger.warning("⚠️ Could not look for interrupted runs (%s)", exc)
        return 0

    recovered = 0
    for key in keys:
        key = key.decode() if isinstance(key, bytes) else str(key)
        run_id = key.rsplit(":", 1)[-1]
        # Counted BEFORE the write, which clears the key — reading afterwards
        # would always report zero and make a successful recovery look empty.
        staged = read_run(run_id) or {}
        try:
            path = write_run_file(run_id, status="partial")
        except Exception as exc:  # noqa: BLE001 - one bad key must not stop the sweep
            logger.error("💥 Could not recover run %s (%s) — its results stay in "
                         "Redis until the TTL clears them", run_id, exc)
            continue
        if path:
            recovered += 1
            kept = sum(len(staged.get(b) or []) for b in _BUCKETS)
            logger.warning(
                "♻️  Recovered run %s from an interrupted session — %d result(s) "
                "kept and judged, marked partial.", run_id, kept,
            )
    if recovered:
        logger.warning("♻️  Recovered %d interrupted run(s) on startup", recovered)
    return recovered