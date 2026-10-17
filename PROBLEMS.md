# Tesserae — problem tracker

The record of what is wrong, what was fixed, and what was measured to be a
non-problem. Committed, so every change shows up in a diff.

**Every claim below was checked against the code before being written down.** When
something turned out not to be a problem, it says so and gives the number — a
tracker that only accumulates suspicions is worse than none, because it gets used
to choose what to work on.

---

## How we work

1. I identify and explain a problem in plain language.
2. We discuss it. **I do not write code.**
3. You give the signal.
4. I code, verify, show you the diff, and wait for your review.

Decisions already made — do not re-ask these:

| Decision | Ruling |
|---|---|
| Counting | One unit = one distinct ad id seen while scrolling **that** keyword. Not occurrences. |
| "Ads scraped" | Cards laid eyes on. Not unique-to-run, not qualified. |
| Target reached | Never ends a crawl on its own — only shortens the dead-end budget 5 → 3. |
| Dead end before target | 5 empty scrolls, then quit. Reached the real bottom → a healthy finish, not a failure. |
| Quitting because "I know these" | Deleted outright. No such rule. |
| Step cap | Removed. A counter expiring is not a reason to quit. |
| Scroll distance | Fixed 1800px. Never grows — skipping a card is unacceptable. |
| Meta's estimate | **Not** trusted. It over-states: reads ~1,400 and yields 954. Reach the real bottom instead. |
| Scope preference | MVP. Skip anything that is over-engineering. |
| Testing | Small keywords only (`rust developer`, ~190). A high-volume one costs many minutes and proves nothing extra. |

---

## Open — ranked by leverage

### 1. Nothing catches a page that fails to render

The entire Search page rendered **blank, on every load**, with no build error, no
lint error and no failing test:

```js
useEffect(() => { writeDraft(formData); }, [formData, lens]);
const [lens, setLens] = useState(null);   // declared 4 lines later
```

A dependency array is evaluated during render, so reading `lens` before its `const`
threw `Cannot access 'lens' before initialization` and the error boundary took the
page down. Found by accident, while verifying the date filters — because the page
had to render for any of that to be checkable.

All six pages render now (audited 2026-10-17). But **the class is invisible**: a
component that throws while rendering gives a white screen and nothing else.

There are no frontend tests at all. A check that opens each page and fails if it
renders nothing would have caught this instantly, and costs about a second.

### 2. The cheap sieve can silently throw away real leads

`passes_heuristics` is a **hard gate in front of the AI** — an ad that fails never
reaches the model, and nobody is told. `career_jobs` carries 29 fixed phrases.

Live measurement, `rust developer`: **1 of 33 new ads reached the AI.**

The filter is earning its keep (it correctly dropped 51 real-estate, medical and
investment ads). But its vocabulary is loose in both directions:

- **False pass** — `"looking for a"` matched *"Looking for a home in Lucknow?"* and
  *"Looking for a Reliable Container House Supplier"*. Two of three ads that got
  through were not jobs.
- **Missed lead** — a real ad phrased outside those 29 phrases ("Rust Engineer —
  Remote", "Rust openings available") is discarded before the AI sees it. Silent.

Work parked by request: tuning the vocabulary. **Not** parked, and cheap: *count what
the sieve drops and show it on the run*, so a keyword where it is eating everything
is visible instead of silent.

### 3. An unvalidated category crashes the orchestrator

`modules/orchestrator/main.py:390` — `CATEGORIES[category_id].description` raises
`KeyError` for an unknown category. Reachable by a direct API call.

Same shape as the date validation fixed today, which now refuses at the API edge.

### 4. Redis log noise — 28 tracebacks per suite run

`modules/verdicts.py` `_remove` does `srem(key, *sorted(doomed & before))`. When
that intersection is empty, Redis raises. It is empty on **every verdict
correction**, because `set_verdict` clears all three buckets before assigning one.
`safe_redis_operation` swallows it into a logged traceback.

Narrower than first recorded: corrections only, not normal runs. The work still
succeeds. One-line guard.

Harmless and genuinely costly — it trains you to ignore stack traces, which is
exactly how a real one gets missed.

### 5. The saved filter selection is git-tracked and written by two processes

`interface/.tesserae_filters.json` is tracked, and written by both the frontend and
the backend with no locking — last writer wins. It has been dirty in most sessions
here, which is why it keeps almost getting committed by accident.

It also carries fields the backend no longer understands; those are now stripped on
read and named in the log (`_REMOVED_FIELDS` in `interface/select_filters.py`).

### 6. CLI defects

- `--clis` is documented and does not exist.
- `_select_platforms()` recurses on invalid input with no depth limit — the comment
  in the code admits it.
- `--cycles 2` reported to hang on a stdin prompt (run-mode prompt runs before the
  "did the user pass args" check). **Unverified.**

### 7. CORS is an invalid combination, and unnecessary

`allow_origins=["*"]` with `allow_credentials=True` (`interface/main.py:62`). Invalid
together, and the browser never talks to :8000 directly anyway — it goes through
the Next proxy.

### 8. Frontend hygiene

- **No frontend tests** — see #1.
- Legacy `frontend/.eslintrc.json` alongside eslint v9 + `eslint-config-next@16`
  against `next@14`. Two configs.
- Run-file parsing duplicated in Python *and* Node; Python's `/api/runs/{id}`
  returns `data` nested and the frontend ignores it entirely.

### 9. Docs and hygiene

- `collection.py` has **two stacked module docstrings** (bad-edit leftover) — lines
  1–27 and 29–44.
- `ExclusionPrompt.disposition` is produced by the reviewer and read by nothing.
- Stale `smoke_test*.json` at `data/ads/` root where nothing reads them.

---

## Measured — NOT problems

Kept so nobody spends time on them again.

| Was reported as | Measurement |
|---|---|
| "`record_results` reloads all four sets per judged batch" | `FilterIndex` reload takes **4.6 ms**. A 1,400-ad keyword does it 70 times: **0.3 s**. Non-issue at any real scale. |
| "The Decisions page re-reads every run file on every lookup" | It caches, keyed on each file's mtime + size. A whole run logged **zero rebuilds**. The ~400 ms page load is row-building, not disk. |
| "Heuristic patterns are merged three times" | `combined_for_run` is called **once**. The described triple-merge no longer exists. What remains is idempotent redundancy. |
| "`run_states` is an unbounded in-memory dict" | ~2 KB per run. A thousand runs is 2 MB. Mitigated anyway: results are now written per run, so losing the dict on restart costs nothing. |
| "`date_preset` and `results_limit_per_keyword` do nothing" | **True — and now removed**, not left inert. |

---

## Parked — at the user's request

- **`advertiser` filter** (search within one page). Real, and useful, but Meta only
  offers it through its own type-ahead dropdown rather than a URL, so it needs UI
  automation. `apply_filters` now names it as *not sent* rather than pretending.
- **Tuning the heuristic vocabulary.** See #2.

---

## Fixed

Each with the commit that did it and the measurement that justified it.

### ✅ The crawl gave up on every keyword after one screen · `53d3175`

Meta lazy-loads: reach the bottom and the next batch is fetched on the spot, with a
spinner or a "see more" while it comes. The page does not move until it lands. The
crawler asked milliseconds later, could not tell busy from finished, and called it
the end.

Live, `python developer`: **25 ads** from 15 scrolls, with **12 of those 15**
reporting "stopped moving" while Meta was still handing out results. After:
**954**, which patient scrolling with *no budget at all* proved was the true bottom
(`python developer` reads ~1,400; the header over-states).

Both signals honoured, found by accessibility markers and wording — never a Meta
class name, which is what killed the call-to-action lookup. Only trusted below the
last ad: an in-ad "see more" and a video ad's buffering spinner are both real and
both false alarms.

### ✅ The results file was rewritten in full after every group · `50a708a`

JSON cannot be appended to. To add one ad you rewrite everything already there, so
the file was rewritten after every judged group of 20. **2 MB of results, 95 MB
written** for a 1,400-ad keyword, because the 70th save added 20 ads by
re-serialising 1,400.

Now appended to Redis and written **once** at the end: 1,400 ads stage in
**0.034 s**, file written once at 362 KB. The staged copy is dropped only after the
write is confirmed — verified by simulating a disk failure. A crashed run's results
are recovered on the next start, because a dead app has nothing left running to
notice them.

### ✅ A keyword held every ad until it finished · `884ab30`

One keyword returns up to 549 ads. All of them sat in memory, nothing reached the
screen, and a crash lost every one. Now two buffers, both 20 — one counts ads read,
the other ads that survived the cheap filters — and groups are handed over
mid-crawl. Live `rust developer`: groups of **27 at 12 s, 21 at 31 s, 7 at 84 s**.

Low risk by construction: the judge already only ever saw 10 ads at a time, so this
changes *when* the model is asked, never *what*.

### ✅ Progress was measured and thrown away · `9c6a913`

The backend reported cards-seen, ads-found and ads-judged on every scroll step. The
Next route builds its response from a whitelist, so all of it was dropped silently.
The one visible number was whole-run percentage, which does not move during a large
keyword — so a working run looked exactly like a dead one.

### ✅ An "abandoned" run was never actually stopped · `678ffe6`

Marking the state released the pipeline and left the crawl running: nothing held a
handle to the task. A second run could start beside it, and when it finished it
overwrote its own status with `completed`. Now the idle path sets the stop flag, so
the run keeps its ads, judges them, saves them and closes cleanly — reported as its
own status, `partial`.

Judging also reports per batch now. It used to be mute, which mattered once keywords
started sending hundreds of ads: minutes of correct silence looked like a hang.

### ✅ A run's results were written by one process while another read them · `2254f7f`

The Search page offered filters that were collected, saved, shown back, and dropped
at URL-build time — with no error. Two date dropdowns sat side by side offering the
same three options, and the first one did nothing.

`start_date[min]`/`[max]` are Meta's own params, verified live by reading Meta's
counts: unfiltered 200, last 7 days 190, today only 110. Now a real "Posted
from"/"Posted to" range, one date control instead of two contradictory ones, and
`apply_filters` says what reached Meta and names what did not.

### ✅ A blank Search page · `79de98b`

See #1 — the cause, fixed. The gap that let it hide is still open.

### ✅ Stopping a run, one at a time · `103b26b`, and the reader · `88adc78`

A Stop button, a cooperative stop that keeps what it collected, a one-run-at-a-time
gate, and a read of the results page in **one** call instead of one per card — the
old way made 20,494 reads where 153 would do, which is the freeze on big keywords.
That fix also uncovered a call-to-action lookup that had matched **nothing** for the
life of the file.

---

## Known caveats — accepted

- The fixed 1800px step is conservative but not *proven* against every card height.
- `restore_scroll_depth` can land at 0 if the tab has not re-rendered a scrollable
  pane yet. Logged loudly; the crawl self-corrects.
- `MAX_STUCK_RELOADS = 3` — raise it if real tabs wedge more than that.
- An in-process watchdog cannot catch a Python loop that spins and blocks the event
  loop. Only restarting the process would. Playwright and the AI client are all
  genuinely async, so the realistic hangs are covered.
- Judging a group of one still costs a full AI round-trip. That is the price of not
  discarding it.