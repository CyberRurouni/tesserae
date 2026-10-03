"""
Checkpoints city — the three gates of judgement.

The ONLY place in Tesserae where AI touches the pipeline. The scraper city
gathers raw material with zero judgement; these gates decide what survives:

  (a) relevance.py  — is this ad actually what the user is looking for?
      (intent = the selected category's description)
  (b) keywords.py   — (future) generate the next keyword batches
  (c) coverage.py   — (future) pool review -> merged exclusion prompts

Every gate is a thin, testable function: AdRecords (or keywords) in,
verdicts out. No scraping here, no storage here — cities stay separate.
"""

from .relevance import judge_ad_relevance

__all__ = ["judge_ad_relevance"]
