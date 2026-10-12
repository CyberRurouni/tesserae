"""
One-time migration: legacy profile-hash verdicts -> the four sets.

Reads   db1 `tesserae:seen_ad:*`        (the only surviving verdict record)
        data/ads/<category>/stored_ads.json   (the authoritative accepted list)
        data/ads/<category>/run_*.json        (ad text + reasons)
Writes  the four sets + their JSON mirrors.

The legacy keys are LEFT ALONE. They are the only copy of the original verdict
history, and nothing reads them once the scraper is on the new sets.

Usage:
    python3 -m scripts.migrate_legacy_verdicts            # dry run, writes nothing
    python3 -m scripts.migrate_legacy_verdicts --apply    # do it
"""

import argparse
import json
import logging
import pathlib
import subprocess
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from genesis.config import ADS_DIR  # noqa: E402

from modules.verdicts import (  # noqa: E402
    record_accepted,
    record_junk,
    record_rejected,
)

logger = logging.getLogger("migrate")

CATEGORY = "career_jobs"
# The active profile owns per-profile rejections. Global sets are unaffected.
PROFILE_ID = "profile_default"


def legacy_ids() -> set[str]:
    out = subprocess.run(
        ["redis-cli", "-n", "1", "--scan", "--pattern", "tesserae:seen_ad:*"],
        capture_output=True, text=True,
    ).stdout.split()
    return {k.split(":")[5] for k in out}


def ads_by_id(category: str) -> dict:
    recs = {}
    for p in sorted((ADS_DIR / category).glob("run_*.json")):
        data = json.loads(p.read_text())
        for bucket in ("accepted", "judged_rejected", "ai_failed", "deferred"):
            for ad in data.get(bucket) or []:
                ad_id = ad.get("ad_archive_id")
                if ad_id and (ad.get("ad_text") or "").strip():
                    recs.setdefault(ad_id, ad)
    return recs


def stored_accepted(category: str) -> set[str]:
    path = ADS_DIR / category / "stored_ads.json"
    if not path.exists():
        return set()
    data = json.loads(path.read_text())
    return {a["ad_archive_id"] for a in data if isinstance(a, dict) and a.get("ad_archive_id")}


def classify(ids, recs):
    """Ask the same question the new judge asks: is an employer hiring anyone?"""
    import asyncio

    from core import AIClient, CATEGORIES, DEFAULT_CATEGORY_ID

    system = (
        "You classify job advertisements for a lead-generation system.\n\n"
        f"CATEGORY: {CATEGORIES[DEFAULT_CATEGORY_ID].label}\n"
        "QUESTION: Does this ad offer actual employment to anyone?\n\n"
        '  "no_hiring"  — nobody is being employed. The ad sells or promotes a course,\n'
        "                 bootcamp, certification, webinar, B2B/recruiting/outsourcing\n"
        "                 or staffing service, a job board, a marketplace, an agency\n"
        "                 product, or content such as a podcast, talk or newsletter.\n"
        '  "not_for_me" — a real employer IS offering a real job to hirees. It may be\n'
        "                 the wrong role, seniority, stack, location, or employment type,\n"
        "                 but someone is hiring someone.\n\n"
        "Judge ONLY whether an employer is hiring. Do not consider whether the role suits\n"
        "any particular person — that is a separate decision made elsewhere.\n\n"
        "A company advertising that it recruits for OTHER companies is \"no_hiring\": the\n"
        "company in the ad is a service provider, not an employer.\n\n"
        'Answer with JSON only: {"no_hiring": true|false, "reason": "<max 10 words>"}\n'
    )

    client = AIClient()
    gate = asyncio.Semaphore(6)

    async def one(ad_id):
        ad = recs[ad_id]
        async with gate:
            try:
                raw = await client.blocking(
                    [
                        {"role": "system", "content": system},
                        {"role": "user",
                         "content": f"ADVERTISER: {ad.get('advertiser_name') or 'unknown'}\n"
                                    f"AD TEXT:\n{ad['ad_text'][:1500]}"},
                    ],
                    max_tokens=200, temperature=0.0,
                )
            except Exception as exc:  # noqa: BLE001
                logger.error("classify failed for %s: %s", ad_id, exc)
                return ad_id, None, ""
        return ad_id, bool(raw.get("no_hiring")), str(raw.get("reason", "")).strip()

    async def go():
        return await asyncio.gather(*(one(i) for i in ids))

    return asyncio.run(go())


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="actually write (default: dry run)")
    parser.add_argument("--mapping", help="reuse a dry-run mapping JSON instead of re-classifying")
    args = parser.parse_args()

    logging.basicConfig(level=logging.INFO, format="%(message)s")

    ids = legacy_ids()
    accepted = stored_accepted(CATEGORY)
    todo = sorted(ids - accepted)

    print("═" * 70)
    print(f"  MIGRATION {'(APPLYING)' if args.apply else '(DRY RUN — writes nothing)'}")
    print("═" * 70)
    print(f"  legacy ids              : {len(ids)}")
    print(f"  accepted (from leads)   : {len(accepted)}   ({len(accepted - ids)} were missing a legacy key)")
    print(f"  to classify             : {len(todo)}")

    if args.mapping:
        blob = json.loads(pathlib.Path(args.mapping).read_text())
        junk = [j["ad_id"] for j in blob["junk"]]
        notme = [j["ad_id"] for j in blob["not_for_me"]]
        dropped = blob.get("no_text", [])
    else:
        recs = ads_by_id(CATEGORY)
        classifiable = [i for i in todo if i in recs]
        dropped = [i for i in todo if i not in recs]
        print(f"  classifiable / no record: {len(classifiable)} / {len(dropped)}")
        print("\n  classifying …")
        results = classify(classifiable, recs)
        junk = [a for a, nh, _ in results if nh]
        notme = [a for a, nh, _ in results if nh is False]

    print(f"\n  → accepted  (global)       {len(accepted)}")
    print(f"  → junk      (global)       {len(junk)}")
    print(f"  → rejected  ({PROFILE_ID}) {len(notme)}")
    print(f"  → dropped (no ad record)   {len(dropped)} {dropped[:5]}")
    print(f"  {'─' * 40}\n  total                  {len(accepted) + len(junk) + len(notme) + len(dropped)}")

    if not args.apply:
        print("\n  dry run only — pass --apply to write")
        return

    print("\n  writing …")
    a = record_accepted(CATEGORY, accepted)
    j = record_junk(CATEGORY, junk)
    r = record_rejected(PROFILE_ID, notme)
    print(f"  accepted +{a}, junk +{j}, rejected +{r}   (0 means already present)")
    print("\n  legacy db1 keys left untouched — nothing deletes them")


if __name__ == "__main__":
    main()