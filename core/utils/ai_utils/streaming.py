"""
Streaming JSON reading — the mechanics behind AIClient.stream().

JsonStreamReader tracks a live depth count of braces/brackets and the
moment a complete, balanced JSON object/array has arrived, it can stop
reading — instead of waiting for the model to keep going until max_tokens
runs out.

build_continuation_prompt builds the message that asks a model to
continue a response that got cut off — used when a stream ends without
ever balancing (see AIClient.stream's continuation loop).
"""

import logging

logger = logging.getLogger("AI UTILS")


class JsonStreamReader:
    """
    Tracks buffer/depth state for a streaming completion, one piece at a
    time, and detects the moment a complete, balanced JSON object or
    array has arrived.

    How it decides "done": a JSON object/array is self-describing about
    when it's finished — every '{' or '[' must be matched by a '}' or ']'.
    So this just counts: +1 every time it sees the opening character, -1
    every time it sees the matching closing character (ignoring anything
    inside a quoted string, since braces there are just text, not structure).
    The moment that count ('depth') returns to zero, the structure is complete.

    The opening/closing character is only ever figured out once, from
    whichever chunk first contains it.

    Used by AIClient.stream() — which feeds it chunks from the native
    Gemini stream and calls _consume() on each — and state is inspected
    between rounds to build continuations for truncated responses.
    """

    def __init__(self):
        self.start_char = None      # '{' or '[' — locked in once, from the first chunk that has one
        self.end_char = None        # the matching '}' or ']'
        self.buffer = ""            # everything received so far (may include leading prose)
        self.depth = 0              # how many start_char are currently unclosed
        self.inside_string = False  # are we inside a "..." value right now?
        self.escape_next = False    # was the previous character a backslash?

    def _consume(self, new_text: str) -> str | None:
        """
        Process one newly-arrived piece of text.

        Returns the finished JSON string (extracted from the buffer) the
        moment it balances, or None while still incomplete.
        """
        offset = len(self.buffer)
        self.buffer += new_text

        for i, ch in enumerate(new_text):
            if self.start_char is None:
                if ch == "{":
                    self.start_char, self.end_char, self.depth = "{", "}", 1
                elif ch == "[":
                    self.start_char, self.end_char, self.depth = "[", "]", 1
                continue

            if self.escape_next:
                self.escape_next = False
                continue

            if ch == "\\" and self.inside_string:
                self.escape_next = True
                continue

            if ch == '"':
                self.inside_string = not self.inside_string
                continue

            if self.inside_string:
                continue

            if ch == self.start_char:
                self.depth += 1
            elif ch == self.end_char:
                self.depth -= 1
                if self.depth == 0:
                    start_index = self.buffer.index(self.start_char)
                    end_index = offset + i + 1
                    return self.buffer[start_index:end_index]

        return None


def build_continuation_prompt(reader: JsonStreamReader) -> str:
    """
    Build the user-turn message that asks the model to continue a response
    that got cut off mid-JSON.

    Deliberately ends the conversation on a USER turn (not a trailing
    assistant message) — Gemini is strict about conversations ending on a
    user turn, so this works rather than relying on prefill support.
    The partial output is still included as an assistant turn right
    before this, so the model can see exactly what it already wrote.
    """
    tail_preview = reader.buffer[-60:]
    return (
        "Your previous response was cut off before the JSON was complete. "
        f"It ended with: ...{tail_preview!r}\n\n"
        "Continue EXACTLY from that point. Do not repeat anything you already "
        "wrote, do not add a fresh opening brace, and do not add any commentary "
        "or code fences. Output only the raw continuation text."
    )
