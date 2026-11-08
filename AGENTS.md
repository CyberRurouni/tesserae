# Tesserae — Assistant Judging Playbook

This file is the standing instruction for the assistant in this repository.
It exists because relevance judging is now a manual step: the assistant runs the
scrape, reads the heuristics-passed ads, and writes the verdicts. Gemini remains
available as a fallback, not as the primary judge. The assistant is the operator;
the frontend is where the user reviews the finished run. Do not ask the user to
start a run from the UI.

Read this file at the start of any judging session. When the user changes the
filtering strategy, the request, or a verdict rule, update this file and say what
changed.

## 1. The workflow

### Scrape

Run the scrape only when the user asks for it. "Run the script" means the whole
cycle: scrape, judge every pending ad, apply, then report the run path, counts,
and `/runs/<run_id>`. Do not hand the user a UI workflow.

### Choose keywords first

Before any scrape, propose **10–12 keywords** and ask the user to approve them.
Do not start scraping until the user has decided the final list. Keep each cycle
to 10–12 keywords; never propose 18+.

Use broad keywords, or specific ones only when the active profile names that
tool/stack. Do not introduce tools the profile does not mention (for example
`n8n`, `make.com`, or `Django`). This keeps sourcing aligned with work the user
can credibly claim. Once the keywords are approved, run the scrape.

```bash
python3 -m interface.manual scrape
```

Useful options:

```bash
python3 -m interface.manual scrape --keywords '["python developer", "backend"]'
python3 -m interface.manual scrape --max-ads-per-keyword 300
python3 -m interface.manual scrape --profile-id <profile_id> --request-id <request_id>
python3 -m interface.manual scrape --parallel-workers 3
```

**Current operator setting (2026-10-31): always pass `--parallel-workers 3`
when running a scrape unless the user says otherwise.** The code default remains
`1`; this is an operating preference, not a default change.

The scrape uses the last saved search filters when no options are given. Options
passed to the CLI also become the saved selection, so the Search page shows them
on the next visit. It applies the category and user heuristics, skips ads with
an existing verdict, and writes:

- `data/agent_audits/pending_<run_id>.json` — profile, request, and every ad
  that survived the heuristics.
- `data/agent_audits/verdicts_<run_id>.json` — a blank verdict template with one
  row per pending ad.
- `data/agent_audits/pending_<run_id>.jsonl` — the streamed raw ads, kept as a
  crash-recovery copy.
- `data/agent_audits/progress_<run_id>.json` — live scrape state. Open
  `/runs/<run_id>` while the scrape runs to watch progress; the file is cleared
  when the verdicts are applied.

No Gemini judging happens in this path. Tell the user the run URL as soon as the
scrape starts so they can watch it; the run page polls the progress file.

### Judge

Read `pending_<run_id>.json` and fill `verdicts_<run_id>.json`.

Judge every pending ad. Do not judge ads that failed the heuristics; they never
reach this file.

Use batches of roughly 25–50 ads so the reasoning stays explicit and the file
can be resumed if the session is interrupted.

### Apply

```bash
python3 -m interface.manual apply <run_id>
```

This validates that every pending ad has exactly one verdict, clears any previous
verdict for those ids, writes the persistent accepted/junk/rejected/deferred
stores, and writes `data/ads/<category>/<run_id>.json`. The existing run page
then shows the result.

Tell the user the run path, the counts, and the run URL
`/runs/<run_id>`.

## 2. What the assistant is judging

There are two stages. The profile stage decides whether the ad belongs to the
person's field at all. The request stage decides whether it fits what they want
right now.

### Profile stage

Ask: **does this ad offer real employment, and is the work this person's field?**

- `accepted` — real employment in the profile's field.
- `judged_rejected` + `rejection_kind: no_hiring` — nobody is being employed:
  courses, bootcamps, certifications, webinars, job boards, marketplaces,
  podcasts, newsletters, or a service sold to employers.
- `judged_rejected` + `rejection_kind: not_for_me` — a real employer is hiring,
  but the work is outside the profile's field or stack.

Rules:

- Judge the actual day-to-day work, not keyword mentions.
- A staffing or placement agency advertising a specific open role for a client
  **is** offering employment. Do not reject it as `no_hiring` just because the
  employer is a client. Reject as `no_hiring` only when the ad sells recruitment
  or staffing services to employers.
- Do **not** reject for seniority, years of experience, location, remote vs
  on-site, company country, contract type, salary, or start date. Those belong
  to the request stage.
- A senior or junior version of the same work is still the same field.
- If genuinely unsure whether the work is in field, accept it and let the
  request stage decide. Reject only when the role is clearly outside the field.

### Request stage

Ask: **does the ad explicitly conflict with what the user asked for right now?**

- `accepted` — no explicit conflict.
- `deferred` — an explicit conflict with a hard request constraint.

Rules:

- **Silence is not a conflict.** If the ad does not state a constraint, do not
  defer for it.
- Only explicit conflicts defer. Soft preferences (`prefer`, `ideally`, `open
  to`) never defer on their own.
- Remote or global ads with no candidate-country restriction are open on
  location. Do not infer a country restriction from the word "remote", from the
  company's headquarters, or from the markets the company serves.
- A company based abroad is not a location conflict unless the ad says the
  candidate must be there or must not be in the requested country.
- Seniority is a request constraint only when the request states a floor or
  ceiling. Defer when the ad's stated range is entirely below the floor or
  entirely above the ceiling; otherwise accept.
- A request can only accept or defer. It can never reject an ad that passed the
  profile stage.

Examples for a request like **"Only Pakistan and in Pakistan only Islamabad"**:

- Defer: `"We are hiring in Mumbai"`, `"Hiring only Europeans"`, `"Now hiring
  Asians, except for Pakistanis"`, `"Remote, Philippines only"`, `"On-site
  Lahore"`.
- Accept: `"We are hiring"`, `"Hiring worldwide"`, `"Looking for candidates in
  Europe and Asia, including India and Bangladesh"`, `"Remote, no location
  restriction stated"`, `"On-site Islamabad"`.

Known ambiguity: on-site Rawalpindi. The profile allows Islamabad or Rawalpindi,
but a request that says "Islamabad only" wins. Treat Rawalpindi as a deferral
unless the user explicitly says otherwise.

## 3. Verdict file format

```json
{
  "run_id": "run_20261028_120000",
  "profile_id": "...",
  "request_id": "...",
  "judged_at": "2026-10-28T12:00:00+00:00",
  "verdicts": [
    {
      "id": "1234567890",
      "bucket": "accepted",
      "stage": "request",
      "reason": "Remote sales role; no candidate-country restriction stated.",
      "rejection_kind": "not_for_me",
      "confidence": 1.0,
      "ad_category": "Sales"
    }
  ]
}
```

- `bucket`: `accepted`, `deferred`, or `judged_rejected`. The aliases `accept`,
  `defer`, `reject`, and `rejected` are accepted by the apply step.
- `stage`: `profile` or `request`. Defaults to `profile` for a rejection and
  `request` otherwise.
- `reason`: required, short, and specific. Name the evidence.
- `rejection_kind`: `no_hiring` or `not_for_me`; only meaningful for a rejection.
- `confidence`: manual verdicts default to `1.0`.
- `ad_category`: a short label such as `Backend Development`, `Sales`,
  `Customer Support`, or `Training`.

## 4. User corrections

When the user says "accept", "defer", or "reject" for a specific ad:

1. Change that ad's verdict and reason.
2. Re-apply the affected run, or the affected ads, as appropriate.
3. If the correction reveals a general rule, update this file with the rule and
   a generic example.
4. If the correction is specific to one ad, do not turn it into a global rule
   without asking.
5. Say plainly what rule was added or changed.

The user may correct the assistant. Treat that as expected and update the
playbook rather than defending the previous verdict.

## 5. Maintenance rules

- Read the active profile and request from the store at the start of every
  session. They are data, not rules; do not hardcode them here.
- When the user changes the request text, re-read it and judge against the new
  text. Do not edit this file unless the user also states a new general policy.
- When the user changes heuristics, the scrape path changes automatically. Do
  not edit this file unless the change also alters judging policy.
- When the user adds or changes a filtering strategy, add a dated entry under
  "User policy changes" below.
- Keep examples generic enough to survive a request edit.
- Never commit `data/`, `browser_profile/`, `exports/`, logs, or `.env`.

## 6. User policy changes

- 2026-10-28 — Manual judging replaces Gemini as the primary judge. Gemini
  prompts remain as a fallback and were corrected to match this playbook.
- 2026-10-28 — Request judging follows the explicit-conflict rule: silence is
  not a conflict; only explicit contradictions defer.
- 2026-10-28 — Profile judging no longer rejects for seniority, location,
  remote/on-site, company country, contract type, salary, or start date.
- 2026-10-28 — Placement/staffing agencies advertising real roles are treated
  as employment ads, not `no_hiring` services.
- 2026-10-28 — The assistant runs the full scrape → judge → apply cycle from the
  CLI. The frontend is the results viewer; the user does not need to start runs
  from the UI.
- 2026-10-31 — Current scrape parallelism is 3 workers. Pass
  `--parallel-workers 3` on every scrape unless the user changes it. The code
  default stays `1`.
- 2026-11-08 — Keyword-first workflow is mandatory: propose 10–12 keywords,
  wait for the user's final list, then scrape. Never propose 12+ per cycle.
  Prefer broad keywords; use specific tools only when the profile names them.
  Ads that mention an unlisted tool can still be accepted during judging if
  they match the profile and request.
