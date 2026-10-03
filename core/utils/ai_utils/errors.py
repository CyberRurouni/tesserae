"""
API-error handling — entirely separate from truncation handling.

Truncation ("the response wasn't finished") is handled inside each case
in client.py, via max_continuations or max_expansions. It is NOT an error
in the sense this file cares about.

This file only deals with the API call itself failing — a bad key, a rate
limit, a dropped connection, a server having a bad moment. Some of those
are worth retrying, some aren't, and the ones worth retrying don't all
deserve the same wait. ApiErrorHandler is the single place that decides.
"""

import asyncio
import logging
import random
from typing import Optional

logger = logging.getLogger("AI UTILS")

try:
    from google.genai import errors as _genai_errors

    _APIError = _genai_errors.APIError
except ImportError:  # SDK missing — classification degrades to "always retry"
    _APIError = None

# HTTP status codes that will fail the same way no matter how many times we
# retry: bad/missing key, permission denied, unknown model/resource, malformed
# request. Everything else (408, 429, 5xx, network errors) is transient.
_NON_RETRYABLE_STATUS_CODES = {400, 401, 403, 404, 422}


def is_retryable(error: Exception) -> bool:
    """True if this error is worth retrying at all; False if it will just fail the same way again."""
    if _APIError is not None and isinstance(error, _APIError):
        status = getattr(error, "code", None)
        return status not in _NON_RETRYABLE_STATUS_CODES
    return True  # unknown error types (timeouts, connection drops) default to retrying


def _extract_retry_after(error: Exception) -> Optional[float]:
    """
    Pull a Retry-After hint straight from the API's response, if it gave one.
    This is authoritative — if the API tells us exactly how long to wait,
    we respect that instead of guessing with backoff.
    """
    response = getattr(error, "response", None)
    if response is None:
        return None

    headers = getattr(response, "headers", None)
    if not headers:
        return None

    value = headers.get("retry-after") or headers.get("Retry-After")
    if value is None:
        return None

    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ApiErrorHandler:
    """
    Owns the retry budget and wait logic for genuine API-call failures —
    entirely separate from a case method's own truncation-handling logic.

    Usage, inside a case method's call loop:

        error_handler = ApiErrorHandler(max_retries=max_retries)
        while True:
            try:
                resp = await client.aio.models.generate_content(...)
            except Exception as e:
                await error_handler.handle(e)   # sleeps, or raises if unrecoverable
                continue                         # if we get here, try again
            ...

    handle() either:
      - raises immediately (non-retryable error, or retry budget exhausted), or
      - waits an appropriate amount of time and returns, signaling "try again"

    Wait logic: if the API gave a Retry-After hint, that's respected exactly.
    Otherwise, exponential backoff with a little jitter (so many concurrent
    calls retrying at once don't all hammer the API in lockstep), capped at
    a sane maximum.
    """

    def __init__(self, max_retries: int = 3, base_delay: float = 1.0, max_delay: float = 30.0):
        self.max_retries = max_retries
        self.base_delay = base_delay
        self.max_delay = max_delay
        self.attempts_used = 0

    async def handle(self, error: Exception) -> None:
        self.attempts_used += 1

        if not is_retryable(error):
            logger.error(f"🚫 Non-retryable API error — failing immediately: {error}")
            raise RuntimeError(f"Non-retryable API error: {error}") from error

        if self.attempts_used > self.max_retries:
            logger.error(
                f"🚨 API error retry budget exhausted ({self.max_retries} retries): {error}"
            )
            raise RuntimeError(f"API error, retries exhausted: {error}") from error

        delay = self._compute_delay(error)
        logger.warning(
            f"💥 API error (retry {self.attempts_used}/{self.max_retries}) "
            f"— waiting {delay:.1f}s before trying again: {error}"
        )
        await asyncio.sleep(delay)

    def _compute_delay(self, error: Exception) -> float:
        retry_after = _extract_retry_after(error)
        if retry_after is not None:
            return retry_after

        # Exponential backoff with jitter, capped
        exp = min(self.base_delay * (2 ** (self.attempts_used - 1)), self.max_delay)
        jitter = random.uniform(0, exp * 0.25)
        return exp + jitter
