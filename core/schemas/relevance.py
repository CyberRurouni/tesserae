"""
AdRelevanceVerdict — one ad's AI judgement at checkpoint (a).

The model returns one verdict per input ad; the checkpoint validates,
dedupes and normalizes them (confidence may arrive as 0-1 or 0-100 —
normalization happens before this model is constructed).

`ai_failed` distinguishes the two rejection kinds downstream:
  False -> the model JUDGED this ad and said no        (final, never re-judged)
  True  -> the model never got to judge it (API error,
           malformed output, missing verdict)          (retried next run)
"""

from pydantic import BaseModel, Field


class AdRelevanceVerdict(BaseModel):
    ad_archive_id: str
    relevant: bool
    confidence: float = Field(default=0.5, ge=0.0, le=1.0)
    reason: str = ""
    # True when the rejection is a CHECKPOINT FAILURE, not a model
    # judgement — such ads deserve another chance on a future run.
    ai_failed: bool = False
