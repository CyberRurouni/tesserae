"""
AIClient — the public Gemini surface of this package.

The rest of Tesserae still calls `.blocking()` and `.stream()` and receives
JSON. Provider details stay here: native `google-genai` requests, JSON
configuration, retries, continuation handling, and optional usage totals.

`track_usage=False` keeps the old plain-dict return shape. With
`track_usage=True`, methods return `(result, usage)` where usage contains
prompt, completion, and total tokens across every round of the task.
"""

import json
import logging
from typing import Any, Dict, Optional

from genesis.config import GEMINI_MODEL
from .errors import ApiErrorHandler
from .parsing import extract_balanced_json, looks_truncated
from .streaming import JsonStreamReader, build_continuation_prompt

logger = logging.getLogger("AI UTILS")

DEFAULT_MODEL = GEMINI_MODEL


class AIClient:
    """Small provider-neutral surface backed by the native Gemini client."""

    def __init__(self, model: str = DEFAULT_MODEL, client: Optional[Any] = None):
        self.model = model
        self._client = client

    @property
    def client(self) -> Any:
        if self._client is None:
            from core import gemini_client

            if gemini_client is None:
                raise RuntimeError("GEMINI_API_KEY is not configured")
            self._client = gemini_client
        return self._client

    @staticmethod
    def _grow_budget(current_tokens: int, increment: int, step_number: int) -> int:
        return current_tokens + (increment * step_number)

    @staticmethod
    def _messages_to_gemini(messages):
        system_parts = []
        contents = []
        for message in messages:
            role = message.get("role", "user")
            content = message.get("content", "")
            if isinstance(content, list):
                content = "\n".join(str(part) for part in content)
            if role == "system":
                system_parts.append(str(content))
                continue
            contents.append(
                {
                    "role": "model" if role == "assistant" else "user",
                    "parts": [{"text": str(content)}],
                }
            )
        return "\n\n".join(system_parts) or None, contents

    @staticmethod
    def _generation_config(max_tokens: int, temperature: float, system_instruction: str | None):
        from google.genai import types

        return types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=temperature,
            max_output_tokens=max_tokens,
            response_mime_type="application/json",
        )

    @staticmethod
    def _usage_values(metadata) -> tuple[int, int, int]:
        if metadata is None:
            return 0, 0, 0
        prompt = int(getattr(metadata, "prompt_token_count", 0) or 0)
        candidates = int(getattr(metadata, "candidates_token_count", 0) or 0)
        thoughts = int(getattr(metadata, "thoughts_token_count", 0) or 0)
        total = int(getattr(metadata, "total_token_count", 0) or 0)
        completion = candidates + thoughts
        return prompt, completion, total or prompt + completion

    @staticmethod
    def _usage_dict(prompt: int, completion: int, total: int) -> Dict[str, int]:
        return {"prompt": prompt, "completion": completion, "total": total}

    @staticmethod
    def _parse_result(text: str):
        if not text or not text.strip():
            return None
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            blob = extract_balanced_json(text)
            if blob:
                try:
                    return json.loads(blob)
                except json.JSONDecodeError:
                    return None
        return None

    # ------------------------------------------------------------------
    # Stream
    # ------------------------------------------------------------------

    async def stream(
        self,
        messages,
        model: Optional[str] = None,
        max_tokens: int = 800,
        increment: int = 200,
        max_continuations: int = 3,
        temperature: float = 0.0,
        max_retries: int = 3,
        track_usage: bool = False,
        fallback: Optional[Dict[str, Any]] = None,
    ):
        try:
            return await self._stream_impl(
                messages,
                model or self.model,
                max_tokens,
                increment,
                max_continuations,
                temperature,
                max_retries,
                track_usage,
            )
        except Exception as e:
            if fallback is not None:
                logger.error(f"🚨 stream() failed — returning fallback. Error: {e}")
                return fallback
            raise

    async def _stream_impl(
        self,
        messages,
        model,
        max_tokens,
        increment,
        max_continuations,
        temperature,
        max_retries,
        track_usage,
    ):
        error_handler = ApiErrorHandler(max_retries=max_retries)
        reader = JsonStreamReader()
        continuation_messages = list(messages)
        continuations_used = 0
        prompt_total = completion_total = usage_total = 0

        while True:
            system_instruction, contents = self._messages_to_gemini(continuation_messages)
            config = self._generation_config(max_tokens, temperature, system_instruction)
            try:
                stream = await self.client.aio.models.generate_content_stream(
                    model=model,
                    contents=contents,
                    config=config,
                )
                async for chunk in stream:
                    prompt, completion, total = self._usage_values(
                        getattr(chunk, "usage_metadata", None)
                    )
                    prompt_total += prompt
                    completion_total += completion
                    usage_total += total

                    text = getattr(chunk, "text", None)
                    if not text:
                        continue
                    blob = reader._consume(text)
                    if blob is not None:
                        result = self._parse_result(blob)
                        if result is None:
                            raise ValueError("Gemini stream produced invalid JSON")
                        if not track_usage:
                            return result
                        return result, self._usage_dict(
                            prompt_total, completion_total, usage_total
                        )
            except Exception as e:
                await error_handler.handle(e)
                continue

            if not reader.buffer or continuations_used >= max_continuations:
                raise ValueError(
                    f"JSON never balanced after {continuations_used} continuation(s)"
                )

            continuations_used += 1
            max_tokens = self._grow_budget(max_tokens, increment, continuations_used)
            logger.info(
                f"↪️ Response cut off — continuation {continuations_used}/{max_continuations} "
                f"| new max_tokens={max_tokens}"
            )
            continuation_messages = list(messages) + [
                {"role": "assistant", "content": reader.buffer},
                {"role": "user", "content": build_continuation_prompt(reader)},
            ]

    # ------------------------------------------------------------------
    # Blocking
    # ------------------------------------------------------------------

    async def blocking(
        self,
        messages,
        model: Optional[str] = None,
        max_tokens: int = 800,
        increment: int = 200,
        max_expansions: int = 3,
        temperature: float = 0.0,
        max_retries: int = 3,
        track_usage: bool = False,
        fallback: Optional[Dict[str, Any]] = None,
    ):
        try:
            return await self._blocking_impl(
                messages,
                model or self.model,
                max_tokens,
                increment,
                max_expansions,
                temperature,
                max_retries,
                track_usage,
            )
        except Exception as e:
            if fallback is not None:
                logger.error(f"🚨 blocking() failed — returning fallback. Error: {e}")
                return fallback
            raise

    async def _blocking_impl(
        self,
        messages,
        model,
        max_tokens,
        increment,
        max_expansions,
        temperature,
        max_retries,
        track_usage,
    ):
        error_handler = ApiErrorHandler(max_retries=max_retries)
        expansions_used = 0
        prompt_total = completion_total = usage_total = 0

        while True:
            system_instruction, contents = self._messages_to_gemini(messages)
            config = self._generation_config(max_tokens, temperature, system_instruction)
            try:
                response = await self.client.aio.models.generate_content(
                    model=model,
                    contents=contents,
                    config=config,
                )
            except Exception as e:
                await error_handler.handle(e)
                continue

            prompt, completion, total = self._usage_values(
                getattr(response, "usage_metadata", None)
            )
            prompt_total += prompt
            completion_total += completion
            usage_total += total

            text = response.text or ""
            result = self._parse_result(text)
            if result is not None:
                if not track_usage:
                    return result
                return result, self._usage_dict(prompt_total, completion_total, usage_total)

            if not text or looks_truncated(text):
                if expansions_used >= max_expansions:
                    raise ValueError(
                        f"Response still truncated after {expansions_used} expansion(s)"
                    )
                expansions_used += 1
                max_tokens = self._grow_budget(max_tokens, increment, expansions_used)
                logger.warning(
                    f"🔪 Response looks truncated — expansion {expansions_used}/{max_expansions} "
                    f"| new max_tokens={max_tokens}"
                )
                continue

            raise ValueError("Invalid JSON from Gemini (not a truncation issue)")
