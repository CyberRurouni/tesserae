"""
Regression suite for the four-set dedup model.

Self-contained: no pytest, no network, no browser. Run with:

    python3 tests/regression.py

Every filesystem path is redirected by setting TESSERAE_DATA_DIR BEFORE the
app is imported, so this can never touch the real data directory. Redis is
shared, so everything here uses a dedicated category name.
"""

import asyncio
import contextlib
import os
import sys
import tempfile
from pathlib import Path

# Must happen before genesis.config is imported anywhere.
_SANDBOX = tempfile.mkdtemp(prefix="tesserae-regression-")
os.environ["TESSERAE_DATA_DIR"] = _SANDBOX
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import (  # noqa: E402
    AdRecord,
    AdRelevanceVerdict,
    RunConfig,
    ScrapingFilters,
    UserProfile,
)
import modules.verdicts as V  # noqa: E402
from modules.verdicts import FilterIndex  # noqa: E402

CATEGORY = "regression_test"
PROFILE = "prof_test"
OTHER = "prof_other"

_fails: list[str] = []
_passed = 0


def check(name, cond, extra=""):
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {name}" + (f"  |  {extra}" if extra else ""))
    else:
        _fails.append(name)
        print(f"  FAIL  {name}" + (f"  |  {extra}" if extra else ""))


def ad(i):
    return AdRecord(ad_archive_id=i, source_keyword="kw", ad_text="text", category=CATEGORY)


def v(i, outcome, kind="not_for_me", **kw):
    return AdRelevanceVerdict(ad_archive_id=i, outcome=outcome, rejection_kind=kind, reason="r", **kw)


# ── 1. the four sets ────────────────────────────────────────────────────────
def test_sets():
    print("\n[1] the four sets")
    V.record_accepted(CATEGORY, ["A1", "A2"])
    V.record_junk(CATEGORY, ["J1"])
    V.record_rejected(PROFILE, ["R1"])
    check("accepted recorded", V.is_accepted(CATEGORY, "A1"))
    check("junk recorded", V.is_junk(CATEGORY, "J1"))
    check("rejected recorded", V.is_rejected(PROFILE, "R1"))
    check("nothing lands in two sets at once",
          not (V.is_junk(CATEGORY, "A1") and V.is_accepted(CATEGORY, "A1")))
    check("re-adding is idempotent", V.record_accepted(CATEGORY, ["A1"]) == 0)

    print("\n[2] global vs per-profile")
    check("accepted is global", V.is_accepted(CATEGORY, "A1"))
    check("junk is global", V.is_junk(CATEGORY, "J1"))
    check("rejected is per profile", not V.is_rejected(OTHER, "R1"))

    print("\n[3] JSON mirrors survive a Redis flush")
    import redis

    r = redis.Redis(host="localhost", port=6379, db=2, decode_responses=True)
    cleared = 0
    for key in r.scan_iter(match=f"tesserae:*:{CATEGORY}"):
        cleared += r.delete(key)
    cleared += r.delete(f"tesserae:rejected:{PROFILE}")
    check("Redis really is empty for these sets", cleared > 0 and
          not r.exists(f"tesserae:accepted:{CATEGORY}"),
          f"{cleared} keys deleted")
    check("still answered, from the JSON mirror", V.is_accepted(CATEGORY, "A1"))
    check("junk recovered", V.is_junk(CATEGORY, "J1"))
    check("rejected recovered", V.is_rejected(PROFILE, "R1"))


# ── 2. record_results routes each outcome ───────────────────────────────────
def test_record_routing():
    print("\n[4] record_results routes outcomes to the right set")
    import modules.storage.results as R

    ads = [ad(i) for i in ("n_1", "n_2", "n_3", "n_4")]
    verdicts = {
        "n_1": v("n_1", "rejected", "no_hiring"),
        "n_2": v("n_2", "rejected", "not_for_me"),
        "n_3": v("n_3", "deferred", request_family_id="req_1"),
        "n_4": v("n_4", "accepted"),
    }
    R.record_results(ads, verdicts, CATEGORY, None, run_id="run_R",
                     searched_keywords=["kw"], profile_id=PROFILE, request_family_id="req_1")
    check("no_hiring -> junk", V.is_junk(CATEGORY, "n_1"))
    check("not_for_me -> not junk", not V.is_junk(CATEGORY, "n_2"))
    check("not_for_me -> this profile only", V.is_rejected(PROFILE, "n_2"))
    check("accepted -> accepted", V.is_accepted(CATEGORY, "n_4"))
    check("accepted not also rejected", not V.is_rejected(PROFILE, "n_4"))
    check("deferred in none of the three",
          not (V.is_accepted(CATEGORY, "n_3") or V.is_junk(CATEGORY, "n_3")
               or V.is_rejected(PROFILE, "n_3")))

    print("\n[5] an AI failure records nothing at all")
    R.record_results([ad("n_5")], {"n_5": v("n_5", "deferred", ai_failed=True)},
                     CATEGORY, None, run_id="run_R", profile_id=PROFILE)
    check("ai_failed is not judged", not V.is_rejected(PROFILE, "n_5"))


# ── 3. the filter decision ──────────────────────────────────────────────────
def test_filter():
    print("\n[6] filter order and outcomes")
    idx = FilterIndex(CATEGORY, PROFILE, "req_1").load()
    check("accepted", idx.check("A1") == (True, "accepted"), str(idx.check("A1")))
    check("junk", idx.check("J1") == (True, "junk"), str(idx.check("J1")))
    check("rejected", idx.check("R1") == (True, "rejected"), str(idx.check("R1")))
    check("deferred under the same request", idx.check("n_3") == (True, "deferred"),
          str(idx.check("n_3")))
    check("unknown ad is judged", idx.check("brand_new") == (False, "new"))

    print("\n[7] another profile is unaffected by this profile's rejections")
    other = FilterIndex(CATEGORY, OTHER, "req_1").load()
    check("still skips accepted", other.check("A1")[0] is True)
    check("still skips junk", other.check("J1")[0] is True)
    check("does NOT skip R1", other.check("R1")[0] is False, str(other.check("R1")))

    print("\n[8] a new request brings the deferred ad back")
    same = FilterIndex(CATEGORY, PROFILE, "req_1").load()
    check("same request stays parked", same.check("n_3")[0] is True)
    new = FilterIndex(CATEGORY, PROFILE, "req_2").load()
    check("different request re-judges",
          new.check("n_3") == (False, "deferred_under_another_request"), str(new.check("n_3")))


# ── 4. the judge ────────────────────────────────────────────────────────────
def test_judge():
    print("\n[9] the three-stage judge")
    import modules.checkpoints.relevance as rel

    class Stub:
        payload = {}
        prompts = []

        def __init__(self, *a, **k):
            pass

        async def blocking(self, messages, **kw):
            Stub.prompts.append(messages)
            if isinstance(Stub.payload, Exception):
                raise Stub.payload
            return Stub.payload

    original = rel.AIClient
    rel.AIClient = Stub
    try:
        Stub.payload = {"verdicts": [
            {"id": "j_1", "outcome": "rejected", "rejection_kind": "no_hiring"},
            {"id": "j_2", "outcome": "rejected", "rejection_kind": "not_for_me"},
            {"id": "j_3", "outcome": "deferred"},
            {"id": "j_4", "outcome": "accepted"},
            {"id": "j_5", "outcome": "rejected", "rejection_kind": "GARBAGE"},
        ]}
        ads = [AdRecord(ad_archive_id=i, source_keyword="kw", ad_text="t") for i in
               ("j_1", "j_2", "j_3", "j_4", "j_5")]
        out = asyncio.run(rel.judge_ad_relevance(ads, intent="jobs", additional_filters="r",
                                                 profile_text="p", request_family_id="req_1"))
        check("no_hiring parsed", out["j_1"].rejection_kind == "no_hiring")
        check("not_for_me parsed", out["j_2"].rejection_kind == "not_for_me")
        check("accepted", out["j_4"].outcome == "accepted")
        check("garbage kind falls to the safe end", out["j_5"].rejection_kind == "not_for_me")
        check("stage 1 asks about employment",
              any("offer actual employment to anyone" in m["content"] for m in Stub.prompts[-1]))

        Stub.payload = RuntimeError("model down")
        out = asyncio.run(rel.judge_ad_relevance(ads[:1], intent="jobs", additional_filters="r"))
        check("model failure DEFERS, never rejects", out["j_1"].outcome == "deferred",
              out["j_1"].outcome)
    finally:
        rel.AIClient = original


# ── 5. the scraper ──────────────────────────────────────────────────────────
def test_scraper():
    print("\n[10] the scraper skips what the filter says")
    import modules.scraper.main as sm
    from modules.scraper.seen import RunSeenIds

    POPULAR = ["p_0", "p_1", "p_2"]
    keywords = [f"kw{i}" for i in range(6)]

    async def scroll(page):
        return None

    async def search(page, keyword, filters, dismiss_cookies=False):
        return None

    async def apply_filters(page, filters):
        return None

    async def empty(page):
        return False

    async def estimate(page):
        return 40

    async def extract(page, source_keyword=None, seen_ids=None):
        seen = seen_ids if seen_ids is not None else set()
        out = []
        for i in POPULAR + [f"u_{source_keyword}_{n}" for n in range(2)]:
            if i in seen:
                continue
            seen.add(i)
            out.append(AdRecord(ad_archive_id=i, source_keyword=source_keyword, ad_text="t"))
        return out

    async def collect(page, tag):
        return None

    saved = {n: getattr(sm, n) for n in
             ("scroll_step", "search_keyword", "apply_filters", "empty_results",
              "read_results_estimate", "extract_ads_from_page", "collect_page_html")}
    sm.scroll_step, sm.search_keyword, sm.apply_filters = scroll, search, apply_filters
    sm.empty_results, sm.read_results_estimate = empty, estimate
    sm.extract_ads_from_page, sm.collect_page_html = extract, collect

    class P:
        async def close(self):
            return None

    class Ctx:
        async def new_page(self):
            return P()

    idx = FilterIndex(CATEGORY, PROFILE, "req_1").load()
    filters = ScrapingFilters(category=CATEGORY, keywords=keywords)

    try:
        fresh = asyncio.run(sm._scrape_on(Ctx(), filters, None, CATEGORY,
                                          RunSeenIds(run_id=None), idx, PROFILE, "req_1", 1))
        got = [a.ad_archive_id for a in fresh]
        check("no duplicate ids in a run", len(got) == len(set(got)), f"{len(got)} rows")
        check("accepted/junk/rejected ads never reach the AI",
              not ({"A1", "J1", "R1"} & set(got)), str(sorted({"A1", "J1", "R1"} & set(got))))

        fresh3 = asyncio.run(sm._scrape_on(Ctx(), filters, None, CATEGORY,
                                            RunSeenIds(run_id=None), idx, PROFILE, "req_1", 3))
        got3 = [a.ad_archive_id for a in fresh3]
        check("parallel yields the same set as sequential", set(got3) == set(got),
              f"{len(set(got3))} vs {len(set(got))}")
        check("parallel introduces no duplicates", len(got3) == len(set(got3)))
    finally:
        for n, f in saved.items():
            setattr(sm, n, f)


# ── 6. cycle semantics unchanged ────────────────────────────────────────────
def test_cycles():
    print("\n[11] cycle semantics are untouched")
    import modules.orchestrator.main as om

    calls = {"gen": 0, "searches": []}

    @contextlib.asynccontextmanager
    async def browser(headless=False):
        yield object()

    async def scrape(filters, headless=False, context=None, run_seen_ids=None,
                     profile_id=None, request_family_id=None, parallel_workers=1):
        calls["searches"].append(list(filters.keywords))
        return []

    async def judge(ads, **kw):
        return {}

    class Batch:
        def __init__(self, kws):
            self.keywords = kws

    async def gen(request):
        calls["gen"] += 1
        return Batch(["ai a", "ai b"] if calls["gen"] < 2 else ["ai c"])

    class T:
        reuse_after_days, reason = 3, "t"

    async def timings(stats):
        return {s.keyword: T() for s in stats}

    async def prompt_timings(r):
        return []

    class Coll:
        def load_collection(self, c):
            return {"keywords": [], "prompts": []}

        def harvest_active(self, c):
            return False

        def save_keyword(self, *a, **k): pass
        def save_prompt(self, *a, **k): pass
        def delete_keywords(self, *a, **k): pass
        def set_harvest_flag(self, *a, **k): pass

    saved = {n: getattr(om, n) for n in
             ("browser_session", "run_scrape", "judge_ad_relevance", "record_results", "coll", "actors")}
    om.browser_session, om.run_scrape = browser, scrape
    om.judge_ad_relevance = judge
    om.record_results = lambda *a, **k: {"accepted": []}
    om.coll = Coll()
    om.actors = type("A", (), {"generate_keywords": staticmethod(gen),
                               "assign_keyword_timings": staticmethod(timings),
                               "assign_prompt_timings_parallel": staticmethod(prompt_timings),
                               "review_coverage": staticmethod(timings)})

    # The orchestrator looks the category up in CATEGORIES, so the cycle test
    # needs a real one. record_results is stubbed, so nothing is written.
    cycle_category = "career_jobs"
    provided = ["python developer", "ai automation specialist"]
    cfg = RunConfig(profile=UserProfile(background="py"), profile_id=PROFILE,
                    filters=ScrapingFilters(category=cycle_category, keywords=provided))
    try:
        t = asyncio.run(om.run_orchestrator(cfg, headless=False, max_cycles=1,
                                            run_mode="generate", run_id="run_C"))
        check("cycle 1 merges provided + AI batch",
              calls["searches"][0] == provided + ["ai a", "ai b"], str(calls["searches"][0]))
        check("provided keywords searched once", t["cycles"] == 1, str(t["cycles"]))

        calls["searches"].clear()
        cfg2 = cfg.model_copy(update={"filters": ScrapingFilters(category=cycle_category, keywords=provided)})
        asyncio.run(om.run_orchestrator(cfg2, headless=False, max_cycles=2,
                                        run_mode="generate", run_id="run_C"))
        flat = [k for s in calls["searches"] for k in s]
        check("provided never repeats across cycles",
              all(flat.count(k) == 1 for k in provided), str(flat))
        check("cycle 2 is AI only", calls["searches"][1] == ["ai c"], str(calls["searches"][1]))

        calls["searches"].clear()
        g0 = calls["gen"]
        cfg3 = cfg.model_copy(update={"filters": ScrapingFilters(category=cycle_category, keywords=provided)})
        asyncio.run(om.run_orchestrator(cfg3, headless=False, max_cycles=5,
                                        run_mode="existing", run_id="run_C"))
        check("existing mode never generates", calls["gen"] == g0)
        check("existing mode searches once and stops", calls["searches"] == [provided],
              str(calls["searches"]))
    finally:
        for n, f in saved.items():
            setattr(om, n, f)


def main():
    print("═" * 62)
    print("  Tesserae regression — sandbox:", _SANDBOX)
    print("═" * 62)
    test_sets()
    test_record_routing()
    test_filter()
    test_judge()
    test_scraper()
    test_cycles()
    print("\n" + "═" * 62)
    if _fails:
        print(f"  {len(_fails)} FAILED: {_fails}")
        return 1
    print(f"  ALL PASS ({_passed} checks)")
    return 0


if __name__ == "__main__":
    sys.exit(main())