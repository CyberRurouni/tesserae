"""
Plain text/JSON parsing helpers — no network, no async, no state.

extract_balanced_json is used by the blocking (non-stream) case to pull a
JSON object/array out of a finished response, possibly wrapped in prose.

_looks_truncated is a heuristic used ONLY by the blocking case, since a
blocking call has no live signal for "did this actually finish" the way
streaming does (see streaming.JsonStreamReader, which tracks depth in real
time and never needs to guess).
"""

from typing import Optional


def extract_balanced_json(text: str) -> Optional[str]:
    """
    Scan `text` for the first complete, balanced JSON object `{...}` or
    array `[...]` and return it as a raw string, or None if nothing
    balanced is found.

    Used as a fallback when the model wraps its JSON in prose or markdown
    fences (e.g. "Sure! Here you go: ```json {...} ```").

    Correctly handles:
      - Nested objects / arrays
      - Escaped characters inside strings (e.g. \\", \\\\)
      - String values that contain braces / brackets
    """
    obj_start = text.find("{")
    arr_start = text.find("[")

    if obj_start == -1 and arr_start == -1:
        return None

    if obj_start == -1:
        start, open_ch, close_ch = arr_start, "[", "]"
    elif arr_start == -1:
        start, open_ch, close_ch = obj_start, "{", "}"
    else:
        if arr_start < obj_start:
            start, open_ch, close_ch = arr_start, "[", "]"
        else:
            start, open_ch, close_ch = obj_start, "{", "}"

    depth = 0
    in_string = False
    escape_next = False

    for i in range(start, len(text)):
        ch = text[i]

        if escape_next:
            escape_next = False
            continue

        if ch == "\\" and in_string:
            escape_next = True
            continue

        if ch == '"':
            in_string = not in_string
            continue

        if in_string:
            continue

        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return text[start : i + 1]

    # Reached end of text without closing the root structure → likely truncated
    return None


# These suffixes strongly suggest the model ran out of tokens mid-generation.
# JSON can't validly end with any of these characters when complete.
_TRUNCATION_SUFFIXES = (
    '"',  # inside a string value
    ",",  # after a key-value pair, expecting more
    ":",  # after a key, value never written
    "[",  # array opened but never closed
    "{",  # object opened but never closed
)


def looks_truncated(text: str) -> bool:
    """
    Return True when the response text looks like it was cut off mid-JSON.
    Blocking-case only — a heuristic guess, not a certainty (compare to
    streaming.JsonStreamReader, which knows for a fact via live depth-tracking).

    Strategy:
    1. Quick suffix check — common last characters of an incomplete generation.
    2. Structural check — if we can't find *any* balanced JSON at all, and the
       text contains at least one opening brace/bracket, assume truncation.
    """
    stripped = text.rstrip()

    if stripped.endswith(_TRUNCATION_SUFFIXES):
        return True

    has_json_opener = "{" in stripped or "[" in stripped
    if has_json_opener and extract_balanced_json(stripped) is None:
        return True

    return False