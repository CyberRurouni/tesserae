"""
Regression suite for the four-set dedup model.

Self-contained: no pytest, no network, no browser. Run with:

    python3 tests/regression.py

Two independent things are redirected, both before any app import:

  * TESSERAE_DATA_DIR — every file the app reads or writes.
  * TESSERAE_REDIS_DB_OFFSET — shifts all three Redis DBs, so this suite gets a
    namespace of its own.

The offset is not optional. Keys are shaped like
tesserae:deferred:<profile_id>:<ad_id>, so a dedicated CATEGORY name gives no
isolation at all — any test that creates a profile writes live keys under the
real prefix and leaves them there. Flushing at the end is safe only because the
databases belong to this run.
"""

import asyncio
import contextlib
import inspect
import json
import os
import sys
import tempfile
import types
from pathlib import Path


def _truthy(value):
    """An already-resolved stand-in for a coroutine the scraper awaits."""
    fut = asyncio.get_event_loop_policy().new_event_loop().create_future()
    fut.set_result(value)
    return fut


# Must happen before genesis.config / redis_utils are imported anywhere.
_SANDBOX = tempfile.mkdtemp(prefix="tesserae-regression-")
os.environ["TESSERAE_DATA_DIR"] = _SANDBOX
os.environ["TESSERAE_REDIS_DB_OFFSET"] = "12"
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from core import (  # noqa: E402
    AdRecord,
    AdRelevanceVerdict,
    RunConfig,
    ScrapingFilters,
    UserProfile,
)
import modules.verdicts as V  # noqa: E402
from modules.profiles.deferred import defer_ad  # noqa: E402
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

    # Use the broker's own database rather than hardcoding one: the suite runs
    # on an offset DB, and connecting to the live one to "test a flush" would
    # delete the user's accepted and junk sets.
    from core.utils.redis_utils import run_state_broker

    db = run_state_broker.connection_pool.connection_kwargs["db"]
    r = redis.Redis(host="localhost", port=6379, db=db, decode_responses=True)
    check("the flush targets a test database, not a live one", db >= 10, f"db={db}")

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
        "n_3": v("n_3", "deferred", request_id="req_1"),
        "n_4": v("n_4", "accepted"),
    }
    R.record_results(ads, verdicts, CATEGORY, None, run_id="run_R",
                     searched_keywords=["kw"], profile_id=PROFILE, request_id="req_1")
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
                                                 profile_text="p", request_id="req_1"))
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

    scrolls = []

    async def scroll(page, distance=4000, baseline=None, ceiling_ms=1500):
        scrolls.append(distance)
        return {"waited_ms": 100, "grew": True, "count": (baseline or 0) + 5, "moved": True}

    async def search(page, keyword, filters, dismiss_cookies=False):
        return None

    async def apply_filters(page, filters):
        return None

    async def empty(page):
        return False

    async def estimate(page):
        return 40

    async def extract(page, source_keyword=None, seen_ids=None, seen_fingerprints=None,
                      encountered=None):
        seen = seen_ids if seen_ids is not None else set()
        prints = seen_fingerprints if seen_fingerprints is not None else set()
        out = []
        for i in POPULAR + [f"u_{source_keyword}_{n}" for n in range(2)]:
            # Progress counts every card on screen, known or not — same rule the
            # real extractor uses for its `encountered` set.
            if encountered is not None:
                encountered.add(i)
            if i in seen:
                continue
            seen.add(i)
            out.append(AdRecord(ad_archive_id=i, source_keyword=source_keyword, ad_text="t"))
            # Mirror the real extractor: identical creative under a new id is
            # dropped rather than reported twice.
            from modules.fingerprint import ad_fingerprint as _fp
            fp = _fp(None, "t")
            if fp in prints:
                continue
            prints.add(fp)
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

    # The scroll distance is the one thing that can silently lose ads. Meta's
    # results list is virtualised, so a jump larger than the renderer can keep
    # up with skips cards that were rendered and never handed to us — and a
    # skipped card is indistinguishable from a card that was never there. The
    # old pacer grew 30% per productive step up to 6000px to save round trips;
    # growth is exactly what could skip, so the distance is now fixed. Every
    # scroll in a crawl must be identical, whatever the last step returned.
    print("\n[10b] the scroll step is fixed, so no scroll can skip past cards")
    from modules.scraper.search import SCROLL_DISTANCE_PX

    check("the distance is conservative", 0 < SCROLL_DISTANCE_PX <= 2000,
          f"{SCROLL_DISTANCE_PX}px")
    check("every scroll in the sequential crawl used the same distance",
          len(set(scrolls)) == 1, f"distances={sorted(set(scrolls))}")
    check("and it is the one constant", set(scrolls) <= {SCROLL_DISTANCE_PX},
          f"{sorted(set(scrolls))} vs {SCROLL_DISTANCE_PX}")

    # One creative, five Library IDs. Real case: Meta issues an id per platform,
    # so a single ad run across Facebook/Instagram/Messenger/Threads/Audience
    # Network/WhatsApp appears six times and dedup by id cannot see it.
    print("\n[10d] one ad under five Library IDs is one ad")
    from modules.fingerprint import ad_fingerprint, normalize_ad_text

    text = ("WE'RE HIRING AN ACCOUNT MANAGER / VIRTUAL ASSISTANT.  "
            "You will support website, automation and AI-related delivery.")
    five_ids = [f"10{i:015d}" for i in range(5)]
    prints = set()
    kept = []
    for ad_id in five_ids:
        fp = ad_fingerprint("Supercharged Talent", text)
        if fp in prints:
            continue
        prints.add(fp)
        kept.append(ad_id)
    check("five ids, one creative, one row", len(kept) == 1, f"{len(kept)} kept")

    check("same text, different advertiser, stays two ads",
          ad_fingerprint("A", text) != ad_fingerprint("B", text))

    check("different text, same advertiser, stays two ads",
          ad_fingerprint("A", text) != ad_fingerprint("A", text + " extra"))

    check("whitespace and case do not make a new ad",
          ad_fingerprint("A", "Hello   WORLD") == ad_fingerprint("a", "hello world"))

    check("zero-width marks do not make a new ad",
          ad_fingerprint("A", "hel﻿lo") == ad_fingerprint("A", "hello"))

    check("an ad with no text has no fingerprint",
          ad_fingerprint("A", "") == "" and ad_fingerprint("A", None) == "")

    check("normalising never returns empty for real text",
          normalize_ad_text("  x  ") == "x")

    # The first-sieve must not be the thing that decides relevance. It used to
    # be require_ad_text_patterns=["hiring"] — an AND gate on one exact word —
    # and a real run dropped 112 of 119 harvested ads before the AI saw any,
    # then reported zero.
    print("\n[10e] the pre-sieve does not decide relevance on one word")
    from core.schemas.category import CATEGORIES, HIRING_VOCABULARY
    from core.schemas.ad import AdRecord as _AR
    from modules.scraper.main import passes_heuristics as _ph
    from core.schemas.filters import ScrapingFilters as _SF

    cat = CATEGORIES["career_jobs"]
    check("the category uses OR, not AND",
          not cat.heuristics.require_ad_text_patterns
          and bool(cat.heuristics.require_any_ad_text_patterns),
          f"and={cat.heuristics.require_ad_text_patterns}")

    def sieve(text):
        return _ph(_AR(ad_archive_id="x", ad_text=text), _SF(category="career_jobs"), cat)

    # Real phrasings that carry no literal "hiring" and were being discarded.
    for phrase in (
        "Join our team of backend engineers.",
        "Now accepting applications for a Python developer role.",
        "Vacancy available: FastAPI developer, remote.",
        "We are looking for a software engineer to join our Berlin office.",
        "Contract role available for an automation specialist.",
    ):
        check(f"kept: {phrase[:38]!r}", sieve(phrase), "dropped by the sieve")

    # Non-jobs still caught cheaply, without the AI.
    check("courses are still excluded",
          not sieve("Enroll in our Python course today."))
    check("an ad with no text is still dropped", not sieve(""))

    # The whole point: a false positive costs one AI call, a false negative
    # costs the lead permanently, so the vocabulary leans permissive.
    check("the vocabulary is not a single word", len(HIRING_VOCABULARY) >= 10,
          f"{len(HIRING_VOCABULARY)} terms")

    # A pane that will not scroll is a fault, not a finding. Counting it toward
    # the quiet budget ends the keyword and reports it as searched — the exact
    # "it just stopped" symptom, and the reason the budget must ignore it.
    print("\n[10c] a stuck pane does not end a keyword")
    import modules.scraper.main as sm2
    from core.schemas.filters import ScrapingFilters as SF

    stuck_calls = {"n": 0}

    async def stuck_scroll(page, distance=1800, baseline=None, ceiling_ms=1500):
        stuck_calls["n"] += 1
        # Moved the first two times, then the pane stopped responding entirely.
        if stuck_calls["n"] > 2:
            return {"waited_ms": 0, "grew": False, "count": baseline, "moved": False}
        return {"waited_ms": 100, "grew": True, "count": (baseline or 0) + 5, "moved": True}

    async def no_ads(page, source_keyword, seen_ids=None, seen_fingerprints=None,
                      encountered=None):
        return []

    class StuckPage:
        """A tab that reloads cleanly but whose pane never recovers."""

        def __init__(self):
            self.reloads = 0

        async def reload(self, wait_until=None):
            self.reloads += 1

        async def wait_for_timeout(self, ms):
            return None

        async def evaluate(self, script, arg=None):
            # The pane cannot be restored — report landing at the top.
            return 0

    stuck_page = StuckPage()

    saved_s = {n: getattr(sm2, n) for n in (
        "scroll_step", "extract_ads_from_page", "empty_results",
        "read_results_estimate", "search_keyword", "apply_filters",
        "collect_page_html", "scroll_depth", "restore_scroll_depth",
    )}
    sm2.scroll_step = stuck_scroll
    sm2.extract_ads_from_page = no_ads
    sm2.empty_results = lambda *a, **k: _truthy(False)
    sm2.read_results_estimate = lambda *a, **k: _truthy(100)
    sm2.search_keyword = lambda *a, **k: _truthy(None)
    sm2.apply_filters = lambda *a, **k: _truthy(None)
    sm2.collect_page_html = lambda *a, **k: _truthy(None)
    sm2.scroll_depth = lambda *a, **k: _truthy(9000)
    sm2.restore_scroll_depth = lambda *a, **k: _truthy(0)
    from modules.scraper.seen import RunSeenIds as _RSI
    try:
        out = asyncio.run(sm2._scrape_keyword(
            stuck_page, "stuck keyword", SF(category="career_jobs", keywords=["stuck keyword"]),
            None, "career_jobs", _RSI(), None, None, None, set(), set(),
            asyncio.Lock(), is_first_keyword=True,
        ))
        # No ads either way; what matters is that it kept trying rather than
        # declaring the bottom after the quiet budget on a dead pane.
        check("a stuck pane is retried, not treated as the bottom",
              stuck_calls["n"] > 3, f"{stuck_calls['n']} scroll attempts")
        # Every reload resets the dead-end tally, so without a bound on reloads
        # this loop could never terminate. It has to give up on the TAB, and say
        # so as a fault rather than as an exhausted keyword.
        check("it reloads rather than abandoning the keyword",
              stuck_page.reloads > 0, f"{stuck_page.reloads} reload(s)")
        check("reloads are bounded, so the crawl terminates",
              stuck_page.reloads <= sm2.MAX_STUCK_RELOADS,
              f"{stuck_page.reloads} vs max {sm2.MAX_STUCK_RELOADS}")
    finally:
        for n, f in saved_s.items():
            setattr(sm2, n, f)


# ── 6. cycle semantics unchanged ────────────────────────────────────────────
def test_cycles():
    print("\n[11] cycle semantics are untouched")
    import modules.orchestrator.main as om

    calls = {"gen": 0, "searches": []}

    @contextlib.asynccontextmanager
    async def browser(headless=False):
        yield object()

    async def scrape(filters, headless=False, context=None, run_seen_ids=None,
                     profile_id=None, request_id=None, parallel_workers=1,
                     on_harvested=None):
        calls["searches"].append(list(filters.keywords))
        ads = [AdRecord(
            ad_archive_id=f"kw_{k}", advertiser_name="A", ad_text="We are hiring",
            source_keyword=k, heuristic_labels=[],
        ) for k in filters.keywords]
        # Mirror the real scraper: report each keyword as it finishes, so the
        # per-keyword judging path is exercised rather than bypassed.
        if on_harvested:
            for i, kw in enumerate(filters.keywords):
                out = on_harvested([a for a in ads if a.source_keyword == kw],
                                   kw, i + 1)
                if inspect.isawaitable(out):
                    await out
        return ads

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
        # The collection is scoped by profile now, so the profile id arrives as
        # a second argument. Recording it is how these tests assert the pool is
        # not shared across profiles.
        scopes = []
        saved = []
        harvest_calls = []

        def load_collection(self, c, profile_id=None):
            Coll.scopes.append(profile_id)
            # "keywords" is everything ever searched; "searchable" is the
            # subset whose cooldown has elapsed. Separate keys, because the
            # generator's memory and the live pool are different questions.
            return {"keywords": [], "searchable": [], "prompts": []}

        def harvest_active(self, c, profile_id=None):
            Coll.scopes.append(profile_id)
            return False

        def merge_with_history(self, stats, *a, **k):
            return list(stats)

        def resolve_harvest(self, searchable, limit=20, chosen=None):
            Coll.harvest_calls.append({"pool": len(searchable),
                                       "limit": limit, "chosen": chosen})
            return [s.keyword for s in searchable], []

        def save_keyword(self, stats, *a, **k):
            Coll.saved.append(getattr(stats, "keyword", None))
            return stats

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

        # The pool is per-profile now. Two profiles must not read each other's
        # keywords — that is what made "full stack AI systems lead" show up in a
        # Python developer run.
        check("collection was read for this run's profile",
              Coll.scopes and all(s == PROFILE for s in Coll.scopes),
              str(sorted(set(Coll.scopes))))
    finally:
        for n, f in saved.items():
            setattr(om, n, f)

    # ── the three bugs that made a run silently skip the user's keywords ──
    print("\n[11b] a run never drops the keywords the user typed")

    saved2 = {n: getattr(om, n) for n in ("browser_session", "run_scrape", "coll")}

    # A judge failure must not discard ads already harvested. This cost an hour
    # of crawling once: three keywords, 429 ads scraped, then a TypeError in the
    # judge call took the whole set with it because nothing was written until
    # every keyword had finished.
    async def scraping_then_failing_scrape(filters, headless=False, context=None,
                                           run_seen_ids=None, profile_id=None,
                                           request_id=None, parallel_workers=1,
                                           on_harvested=None):
        saved_calls = []
        for i, kw in enumerate(filters.keywords):
            ads_here = [AdRecord(ad_archive_id=f"k{i}_{j}", advertiser_name="A",
                                 ad_text="We are hiring", source_keyword=kw,
                                 heuristic_labels=[]) for j in range(2)]
            if on_harvested:
                out = on_harvested(ads_here, kw, i + 1)
                if inspect.isawaitable(out):
                    await out
            saved_calls.append(ads_here)
        return [a for batch in saved_calls for a in batch]

    om.browser_session, om.run_scrape = browser, scraping_then_failing_scrape
    om.coll = Coll()
    try:
        async def exploding_judge(ads, **kw):
            raise TypeError("judge_ad_relevance() got an unexpected keyword argument 'parallel_workers'")

        real_judge = om.judge_ad_relevance
        om.judge_ad_relevance = exploding_judge
        cfg5 = RunConfig(profile=UserProfile(background="py"), profile_id=PROFILE,
                         filters=ScrapingFilters(category="career_jobs",
                                                 keywords=["kw a", "kw b"]))
        try:
            asyncio.run(om.run_orchestrator(cfg5, headless=False, max_cycles=1,
                                            run_mode="existing", run_id="run_J"))
            check("judge failure is not swallowed", False, "no error raised")
        except TypeError:
            check("judge failure propagates rather than looking like success", True)
        finally:
            om.judge_ad_relevance = real_judge
    finally:
        for n, f in saved2.items():
            setattr(om, n, f)

    seen_scrape = {}

    async def scrape2(filters, headless=False, context=None, run_seen_ids=None,
                      profile_id=None, request_id=None, parallel_workers=1,
                      on_harvested=None):
        seen_scrape["keywords"] = list(filters.keywords)
        seen_scrape["workers"] = parallel_workers
        seen_scrape["request_id"] = request_id
        if on_harvested:
            for i, kw in enumerate(filters.keywords):
                out = on_harvested([], kw, i + 1)
                if inspect.isawaitable(out):
                    await out
        return []

    class HarvestColl(Coll):
        def harvest_active(self, c, profile_id=None):
            return True

        def load_collection(self, c, profile_id=None):
            pool = [types.SimpleNamespace(keyword="pool keyword")]
            return {"keywords": pool, "searchable": pool, "prompts": []}

    om.browser_session, om.run_scrape = browser, scrape2
    om.coll = HarvestColl()
    try:
        cfg4 = RunConfig(profile=UserProfile(background="py"), profile_id=PROFILE,
                         filters=ScrapingFilters(category=cycle_category, keywords=provided),
                         parallel_workers=3, request_id="req_H")
        asyncio.run(om.run_orchestrator(cfg4, headless=False, max_cycles=1,
                                        run_mode="generate", run_id="run_H"))
        check("harvest mode still searches the user's keywords",
              all(k in seen_scrape.get("keywords", []) for k in provided),
              str(seen_scrape.get("keywords")))
        check("harvest mode still searches its own pool",
              "pool keyword" in seen_scrape.get("keywords", []),
              str(seen_scrape.get("keywords")))
        check("parallel_workers reaches the scraper", seen_scrape.get("workers") == 3,
              str(seen_scrape.get("workers")))
        # Without this the deferred bucket is never loaded and parked ads get
        # judged again on every run.
        check("request_id reaches the scraper",
              seen_scrape.get("request_id") == "req_H", str(seen_scrape.get("request_id")))
    finally:
        for n, f in saved2.items():
            setattr(om, n, f)


# ── 8. user corrections ─────────────────────────────────────────────────────
def test_corrections():
    print("\n[13] a wrong verdict is undoable, and stays in one bucket")
    import modules.verdicts as vv

    cat = "regression_corrections"
    prof, other = "prof_corr", "prof_corr_other"

    # Assigning must clear the other sets. The filter checks accepted first, so
    # an id left in two sets would keep the first answer and ignore the edit.
    vv.set_verdict(cat, prof, "X1", "junk")
    check("junk only", vv.is_junk(cat, "X1") and not vv.is_accepted(cat, "X1"))
    vv.set_verdict(cat, prof, "X1", "accepted")
    check("accepted only after re-assign",
          vv.is_accepted(cat, "X1") and not vv.is_junk(cat, "X1"))
    vv.set_verdict(cat, prof, "X1", "rejected")
    check("rejected only after re-assign",
          not vv.is_accepted(cat, "X1") and "X1" in vv.rejected_ids(prof))
    check("rejection is profile-scoped", "X1" not in vv.rejected_ids(other))
    check("accepted/junk are global", not vv.is_accepted(cat, "X1"))

    # Deleting must survive a cold Redis mirror: JSON is the truth.
    vv.set_verdict(cat, prof, "X1", "junk")
    check("junk again", vv.is_junk(cat, "X1"))
    vv.forget_junk(cat, ["X1"])
    check("forget removes it", not vv.is_junk(cat, "X1"))
    check("forget survives Redis loss",
          not ("X1" in {i for i in V._load_mirror(f'junk_{cat}')}))

    # Move: copy-then-delete, never widening. An interrupted move must leave the
    # ids under the old profile (inert), not half-applied.
    vv.set_verdict(cat, prof, "M1", "rejected")
    moved = vv.move_rejected(prof, other, ["M1"])
    check("moved the right count", moved == 1, str(moved))
    check("no longer under the old profile", "M1" not in vv.rejected_ids(prof))
    check("now under the new profile", "M1" in vv.rejected_ids(other))
    check("moving to the same profile is a no-op", vv.move_rejected(other, other, ["M1"]) == 0)

    # rejected is the only scoped verdict — assigning it without a profile is
    # an error, not a silent no-op.
    try:
        vv.set_verdict(cat, None, "X2", "rejected")
        check("rejected without a profile is refused", False)
    except ValueError:
        check("rejected without a profile is refused", True)
    try:
        vv.set_verdict(cat, prof, "X2", "nonsense")
        check("unknown outcome is refused", False)
    except ValueError:
        check("unknown outcome is refused", True)


# ── 9. the active pointer binds a request ───────────────────────────────────
def test_active_pointer():
    print("\n[12] an unbound pointer is repaired, and runs never judge blind")
    from modules.profiles import active as act, store as fstore

    profile = fstore.create_profile(about="Regression Pointer", profile_text="p")
    request = fstore.create_request(profile.profile_id, "r")

    # A pointer written before requests existed, on a single-request profile.
    act.set_active(profile.profile_id, None)
    check("sole request is linked", act.active_request_id() == request.request_id)
    check("repair is written to disk",
          json.loads(act.ACTIVE_FILE.read_text())["request_id"] == request.request_id)

    # A known-good pointer is never second-guessed.
    act.set_active(profile.profile_id, request.request_id)
    check("valid pointer survives", act.active_request_id() == request.request_id)

    # The run path must fall back to the active request, or the deferred
    # bucket goes unread and parked ads get judged again on every run.
    cat = "career_jobs_regression_ptr"
    defer_ad(profile.profile_id, {"ad_archive_id": "PARKED"}, request.request_id)
    bound = FilterIndex(cat, profile.profile_id, act.active_request_id()).load()
    check("a bound request keeps parked ads parked", bound.check("PARKED")[0] is True,
          str(bound.check("PARKED")))
    blind = FilterIndex(cat, profile.profile_id, None).load()
    check("which is why an unbound run would re-judge them", blind.check("PARKED")[0] is False)

    # Two requests: the answer is ambiguous, so nothing is guessed.
    fstore.create_request(profile.profile_id, "r2")
    act.set_active(profile.profile_id, None)
    check("ambiguous pointer is left alone", act.active_request_id() is None)

    # A request belonging to another profile must not be honoured — it would
    # judge this profile's ads against someone else's request text.
    stranger = fstore.create_request(
        fstore.create_profile(about="Regression Stranger", profile_text="s").profile_id, "x")
    act.set_active(profile.profile_id, stranger.request_id)
    check("cross-profile request is rejected",
          act.active_request_id() != stranger.request_id)


# ── 10. the keyword record is a life, not a snapshot ────────────────────────
def test_keyword_lifetime():
    """A keyword searched twice must remember both searches, not just the last."""
    print("\n[14] a keyword's record accumulates instead of being overwritten")
    from datetime import datetime, timedelta, timezone

    import modules.orchestrator.collection as C
    from core import KeywordStats

    cat = "keyword_lifetime_test"
    scope = "prof_lifetime"
    when = datetime.now(timezone.utc)
    C.delete_keywords(cat, ["lifetime kw"], scope)

    # Cycle 1 — a good day.
    first = C.save_keyword(
        KeywordStats(keyword="lifetime kw", runs_searched=1, ads_fetched=30,
                     relevant_ads=4, last_relevant_hit_at=when),
        cat, when + timedelta(days=5), profile_id=scope,
    )
    check("cycle 1 is stored", first.runs_searched == 1 and first.relevant_ads == 4,
          f"runs={first.runs_searched} leads={first.relevant_ads}")

    # Cycle 2 — a quiet day, then a partial one.
    C.save_keyword(
        KeywordStats(keyword="lifetime kw", runs_searched=1, ads_fetched=0, relevant_ads=0),
        cat, when + timedelta(days=3), profile_id=scope,
    )
    after_quiet = C.save_keyword(
        KeywordStats(keyword="lifetime kw", runs_searched=1, ads_fetched=12,
                     relevant_ads=1, last_relevant_hit_at=when),
        cat, when + timedelta(days=2), profile_id=scope,
    )

    check("searches accumulate", after_quiet.runs_searched == 3,
          f"runs={after_quiet.runs_searched}")
    check("leads accumulate rather than resetting",
          after_quiet.relevant_ads == 5, f"leads={after_quiet.relevant_ads}")
    check("ads accumulate", after_quiet.ads_fetched == 42,
          f"ads={after_quiet.ads_fetched}")
    check("a keyword that once found leads stays alive",
          after_quiet.status_hint == "alive", str(after_quiet.status_hint))

    # The whole point: a quiet day must not erase a proven keyword. Before this,
    # cycle 2 alone would have read "0 leads" and the leaderboard would have
    # dropped the keyword entirely.
    print("\n[15] the cooldown expires; the record does not")
    check("the record outlives its cooldown",
          C.load_keyword("lifetime kw", cat, scope) is not None)

    live = C.load_collection(cat, profile_id=scope)
    check("a keyword inside its cooldown is not searchable",
          all(s.keyword != "lifetime kw" for s in live["searchable"]),
          f"searchable={[s.keyword for s in live['searchable']]}")
    check("but it is still known", len(live["keywords"]) == 1,
          f"known={len(live['keywords'])}")

    # Once the cooldown lapses it joins the pool again — same record, not a new one.
    C.save_keyword(
        KeywordStats(keyword="lifetime kw", runs_searched=1, ads_fetched=5, relevant_ads=0),
        cat, when - timedelta(days=1), profile_id=scope,
    )
    live = C.load_collection(cat, profile_id=scope)
    check("an expired cooldown makes it searchable again",
          any(s.keyword == "lifetime kw" for s in live["searchable"]),
          f"searchable={[s.keyword for s in live['searchable']]}")
    reopened = live["searchable"][0]
    check("and the accumulated history survived the wait",
          reopened.runs_searched == 4 and reopened.relevant_ads == 5,
          f"runs={reopened.runs_searched} leads={reopened.relevant_ads}")

    check("the record TTL is far longer than any cooldown",
          C.STATS_RECORD_TTL is None,
          "records never expire — the cooldown governs searchability instead")

    # ── the generator's feedback ────────────────────────────────────────────
    print("\n[16] the generator is told what its keywords actually did")
    import modules.orchestrator.actors as A

    check("no history means no feedback section", A._feedback_blocks([]) == "")

    block = A._feedback_blocks([
        KeywordStats(keyword="ai automation specialist", runs_searched=3,
                     ads_fetched=45, relevant_ads=9),
        KeywordStats(keyword="backend developer", runs_searched=1,
                     ads_fetched=24, relevant_ads=0),
        KeywordStats(keyword="python backend developer urgent", runs_searched=1,
                     ads_fetched=0, relevant_ads=0),
    ])
    check("a proven keyword is named as an exemplar",
          "YOUR PROVEN KEYWORDS" in block and "ai automation specialist" in block)
    check("a zero-ad keyword is marked too narrow, not merely 'rejected'",
          "python backend developer urgent" in block and "too narrow" in block)
    check("finds-but-no-leads is distinguished from finds-nothing",
          "none wanted" in block and "0 ads —" in block)
    check("the whole history is shown, not just the winners",
          "EVERYTHING TRIED SO FAR" in block)

    many = [
        KeywordStats(keyword=f"kw {i}", runs_searched=1, ads_fetched=0, relevant_ads=0)
        for i in range(60)
    ]
    big = A._feedback_blocks(many)
    check("a long history is capped so it cannot bury the signal",
          big.count("\n- ") <= C.FEEDBACK_HISTORY_LIMIT + 1,
          f"{big.count(chr(10) + '- ')} lines")
    check("with no winners it says so rather than implying any",
          "none yet" in big)

    # ── the prompt itself ───────────────────────────────────────────────────
    print("\n[17] the prompt teaches the mechanic, not the guesswork")
    sys_prompt = A._GENERATOR_SYSTEM.lower()
    check("it states that Meta ANDs the terms", "ands every word" in sys_prompt)
    check("it frames keywords as advertiser vocabulary",
          "advertiser" in sys_prompt and "job seeker" in sys_prompt)
    check("it cites a verified result count",
          "1,100" in sys_prompt and "780" in sys_prompt)
    check("it cites keywords verified to match nothing",
          "return nothing" in sys_prompt)
    check("it explains WHY terms narrow, not just that they do",
          "earn its place" in sys_prompt)
    # The ban list was removed on purpose: the data contradicts it. The single
    # best keyword in the system's history is "python developer hiring", and
    # "remote" appears on both sides of the ledger. A ban list would suppress a
    # pattern that demonstrably works, so the prompt teaches the mechanism and
    # lets the feedback do the correcting.
    check("no hardcoded ban list", "banned:" not in sys_prompt)
    check("it does not forbid a word its own exemplars use",
          "never add hiring" not in sys_prompt)
    check("the old coaching that caused the problem is gone",
          "hiring context" not in sys_prompt)
    check("the latest batch is compared against the exemplars",
          "most recent batch" in sys_prompt)

    # ── harvest: the user decides, the system proposes ──────────────────────
    print("\n[18] harvest re-searches what earned leads, and the user can override")
    pool = [
        KeywordStats(keyword="quiet one", runs_searched=2, ads_fetched=0, relevant_ads=0),
        KeywordStats(keyword="big winner", runs_searched=3, ads_fetched=90, relevant_ads=12),
        KeywordStats(keyword="fetches plenty", runs_searched=1, ads_fetched=40, relevant_ads=0),
        KeywordStats(keyword="steady", runs_searched=1, ads_fetched=8, relevant_ads=3),
    ]

    ranked = [s.keyword for s in C.rank_for_harvest(pool)]
    check("leads rank first", ranked[0] == "big winner", str(ranked))
    check("a working search outranks one that found nothing",
          ranked.index("fetches plenty") < ranked.index("quiet one"), str(ranked))

    top, _ = C.resolve_harvest(pool, limit=2)
    check("the limit is honoured", top == ["big winner", "steady"], str(top))
    check("a limit of 0 harvests nothing rather than everything",
          C.resolve_harvest(pool, limit=0)[0] == [])

    picked, _ = C.resolve_harvest(pool, limit=2, chosen=["quiet one", "big winner"])
    check("an explicit choice overrides the ranking", picked == ["quiet one", "big winner"],
          str(picked))

    _, notes = C.resolve_harvest(pool, chosen=["cooling down kw", "steady"])
    check("a keyword that is not searchable is refused, not silently searched",
          len(notes) == 1 and "cooling down" in notes[0], str(notes))

    # ── the last batch, identified by its shared instant ────────────────────
    print("\n[19] the generator can see where its last batch drifted")
    from datetime import datetime, timedelta, timezone

    older = datetime.now(timezone.utc) - timedelta(days=2)
    newest = datetime.now(timezone.utc)
    history = [
        KeywordStats(keyword="proven shape", runs_searched=2, ads_fetched=40,
                     relevant_ads=6, last_searched_at=older),
        KeywordStats(keyword="drifted away", runs_searched=1, ads_fetched=0,
                     relevant_ads=0, last_searched_at=newest),
        KeywordStats(keyword="also drifted", runs_searched=1, ads_fetched=0,
                     relevant_ads=0, last_searched_at=newest),
    ]
    batch = C.last_batch(history)
    check("the last batch is the newest instant's keywords",
          {s.keyword for s in batch} == {"drifted away", "also drifted"},
          str([s.keyword for s in batch]))

    block = A._feedback_blocks(history)
    check("the last batch is shown separately", "YOUR MOST RECENT BATCH" in block)
    check("the exemplars are shown beside it", "YOUR PROVEN KEYWORDS" in block)
    check("the batch block tells the model to compare the two",
          "compare it against" in block.lower())
    batch_section = block.split("YOUR PROVEN KEYWORDS")[0]
    check("a keyword searched earlier is not listed as part of the last batch",
          '"proven shape"' not in batch_section, str(batch_section.strip()[-120:]))
    check("it still appears once as an exemplar",
          '"proven shape"' in block.split("EVERYTHING TRIED SO FAR")[0])

    C.delete_keywords(cat, ["lifetime kw"], scope)


# ── 11. one profile, one lens ────────────────────────────────────────────────
def test_single_lens():
    """
    The generator and the judge must read the SAME profile.

    They used to read different things: the generator was handed the profile
    store's text, the judge a separate text file that mirrored it. They agreed
    only while that mirror was maintained — and when it wasn't, a run hunted
    for one person and graded the results for another, silently.
    """
    print("\n[20] one profile, one lens")
    from modules.profiles import active as act, store as fstore
    from modules.storage import resolve_lens

    strong = fstore.create_profile(about="Lens Test Strong", profile_text="STRONG PROFILE TEXT")
    other = fstore.create_profile(about="Lens Test Other", profile_text="OTHER PROFILE TEXT")
    req_strong = fstore.create_request(strong.profile_id, "STRONG REQUEST TEXT")
    req_other = fstore.create_request(other.profile_id, "OTHER REQUEST TEXT")

    # The active pointer names one of them; the caller names the other. An
    # explicit request must win — guessing here is how a run gets judged against
    # somebody else's profile.
    act.set_active(strong.profile_id, req_strong.request_id)

    text, request = resolve_lens(other.profile_id, req_other.request_id)
    check("an explicitly named profile beats the active pointer",
          text == "OTHER PROFILE TEXT", text[:40])
    check("and so does an explicitly named request",
          request == "OTHER REQUEST TEXT", request[:40])

    text, request = resolve_lens()
    check("with no names given, the active pointer is used",
          text == "STRONG PROFILE TEXT", text[:40])
    check("along with its own request", request == "STRONG REQUEST TEXT", request[:40])

    check("an unknown profile falls back rather than judging nothing",
          resolve_lens("does_not_exist")[0] == "STRONG PROFILE TEXT")

    # A request belonging to a different profile must never scope this run's
    # accept-vs-defer decisions to someone else's wishes.
    text, request = resolve_lens(strong.profile_id, req_other.request_id)
    check("a cross-profile request is refused",
          request == "", f"got {request[:30]!r}")

    # ── the file mirror is gone, and cannot come back ──────────────────────
    print("\n[21] there is no second copy of a profile to disagree with")
    import genesis.config as genesis_config

    check("no profile text files are defined anywhere",
          not hasattr(genesis_config, "ABOUT_US_PATH")
          and not hasattr(genesis_config, "ADDITIONAL_FILTERS_PATH"),
          "the txt mirror was the original storage, not a cache")
    check("no hash-of-profile-text helpers remain",
          not hasattr(genesis_config, "compute_profile_hashes"),
          "reword a profile and every judged ad used to look unseen again")

    import modules.profiles.active as active_module
    check("nothing follows a text file into the store",
          not hasattr(active_module, "sync_with_profile_file"),
          "the file used to overwrite the store at the start of every run")

    import modules.scraper.seen as seen_module
    check("the pre-four-sets dedup index is gone",
          not hasattr(seen_module, "SeenIndex"),
          "verdicts are filed against a stable profile id now")

    import core
    check("and the empty verdict database went with it",
          not hasattr(core, "verdict_broker"),
          "Redis DB 1 is gone; the sets live in DB 1")

    import interface.profiles_api as papi
    check("no endpoint writes a profile mirror any more",
          not hasattr(papi, "_sync_profile_file"))

    # ── and both AI steps read the one resolved field ───────────────────────
    print("\n[22] the generator and the judge are handed the same text")
    import modules.orchestrator.main as om
    import inspect

    src = inspect.getsource(om.run_orchestrator)
    check("the lens is resolved once, from the store",
          "resolve_lens(config.profile_id, config.request_id)" in src)
    check("and written into the run config every consumer reads",
          "config = config.model_copy(update={\"profile\": UserProfile(background=about)})" in src)
    judge_src = inspect.getsource(om._search_and_judge)
    check("the judge reads that same field",
          "profile_text=config.profile.background" in judge_src)
    check("the judge reads no text file",
          "load_profile" not in judge_src and "_load_texts" not in src)


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
    test_corrections()
    test_active_pointer()
    test_keyword_lifetime()
    test_single_lens()

    # Leave nothing behind. Safe because TESSERAE_REDIS_DB_OFFSET moved these
    # databases off the ones the app uses.
    _flush_test_redis()

    print("\n" + "═" * 62)
    if _fails:
        print(f"  {len(_fails)} FAILED: {_fails}")
        return 1
    print(f"  ALL PASS ({_passed} checks)")
    return 0


def _flush_test_redis() -> None:
    """Empty the suite's own Redis databases. Never touches live ones."""
    try:
        from core.utils.redis_utils import keyword_broker, run_state_broker

        flushed = 0
        for broker in (run_state_broker, keyword_broker):
            if broker.connection_pool.connection_kwargs.get("db", 0) < 10:
                continue  # would be a live database — refuse rather than risk it
            flushed += broker.flushdb()
        print(f"  🧹 Flushed {flushed} test database(s)")
    except Exception as exc:  # noqa: BLE001 - cleanup must not mask a failure
        print(f"  ⚠️  Could not flush test Redis: {exc}")


if __name__ == "__main__":
    sys.exit(main())