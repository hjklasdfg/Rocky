"""MiniMax LLM client — OpenAI-compatible chat completions with tool calling.

MiniMax exposes an OpenAI-compatible endpoint, so we use the openai SDK with a
custom base_url. This gives us function calling, streaming, and the standard
message format for free.

Pricing (as of refactor date — verify current rates):
- MiniMax-M2:        ~$0.30/1M input, ~$1.20/1M output
- abab6.5s-chat:     ~$0.20/1M input, ~$0.60/1M output

Reference: https://www.minimaxi.com/document/guides/chat-model
"""

from __future__ import annotations

import os
import re
import time
from dataclasses import dataclass, field
from typing import Any

from openai import OpenAI

# MiniMax-M2 emits <think>…</think> reasoning blocks in `content`. Strip them
# before showing to the user (or feeding into a voice synth) — but preserve
# the original text on the LLMResponse for tracing/debugging.
_THINK_BLOCK = re.compile(r"<think>.*?</think>\s*", re.DOTALL | re.IGNORECASE)


def strip_think(text: str | None) -> str | None:
    if not text:
        return text
    cleaned = _THINK_BLOCK.sub("", text).strip()
    return cleaned or text  # fall back to raw if everything was a think block

# Default model for the agent + router. Override per-call if needed.
# MiniMax-M2.7 (released 2026-03-18) is the current flagship — better at
# complex agent workflows than M2, same pricing.
DEFAULT_MODEL = os.getenv("MINIMAX_MODEL", "MiniMax-M2.7")

# Pricing table (USD per 1M tokens). Used by metrics/cost.py.
# Update when MiniMax revises pricing.
# Note: M2.7-highspeed exists as a faster variant at the same price but
# requires a higher Token Plan tier — falls back to M2.7 standard otherwise.
PRICING = {
    "MiniMax-M2.7":           {"input": 0.30, "output": 1.20},
    "MiniMax-M2.7-highspeed": {"input": 0.30, "output": 1.20},
    "MiniMax-M2.7-VL":        {"input": 0.40, "output": 1.20},  # Vision variant; image tokens count toward input
    "MiniMax-M2.5":           {"input": 0.30, "output": 1.20},
    "MiniMax-M2":             {"input": 0.30, "output": 1.20},
    "MiniMax-Text-01":        {"input": 0.20, "output": 1.10},
    "abab6.5s-chat":          {"input": 0.20, "output": 0.60},
}


@dataclass
class LLMResponse:
    """Normalized response wrapper — what callers actually use.

    Wraps the raw OpenAI ChatCompletion to expose only what the agent loop
    needs, plus metadata (latency, cost) for tracing.
    """
    content: str | None
    tool_calls: list[dict] = field(default_factory=list)
    model: str = ""
    prompt_tokens: int = 0
    completion_tokens: int = 0
    latency_ms: int = 0
    cost_usd: float = 0.0
    raw_message: dict | None = None  # OpenAI-format assistant message for history

    @property
    def has_tool_calls(self) -> bool:
        return bool(self.tool_calls)


class MiniMaxClient:
    """Thin wrapper over the OpenAI SDK pointed at MiniMax.

    Single instance shared across all agents. Caller passes messages + tools;
    we handle the HTTP, parsing, and cost calculation.
    """

    def __init__(self, api_key: str | None = None, base_url: str | None = None):
        api_key = api_key or os.getenv("MINIMAX_API_KEY")
        if not api_key:
            raise RuntimeError(
                "MINIMAX_API_KEY not set. Get one at https://www.minimaxi.com/"
            )
        # MiniMax international platform: api.minimax.io (note: .io, not .com).
        # China platform: api.minimax.chat (different keys, different format).
        base_url = base_url or os.getenv(
            "MINIMAX_BASE_URL", "https://api.minimax.io/v1"
        )
        self._client = OpenAI(api_key=api_key, base_url=base_url)

    def chat(
        self,
        messages: list[dict],
        tools: list[dict] | None = None,
        model: str | None = None,
        temperature: float = 0.3,
        tool_choice: str = "auto",
        max_tokens: int | None = None,
    ) -> LLMResponse:
        """Single non-streaming chat completion.

        Args:
            messages: OpenAI-format message list. system, user, assistant, tool roles.
            tools: OpenAI-format tools schema. None = no function calling.
            model: Override the default model.
            temperature: 0.0–1.0. Lower for routing/structured output.
            tool_choice: "auto", "none", or {"type": "function", "function": {"name": "..."}}.
            max_tokens: Cap output length. None = model default.

        Returns:
            LLMResponse with parsed content, tool calls, and cost metadata.
        """
        model = model or DEFAULT_MODEL
        kwargs: dict[str, Any] = {
            "model": model,
            "messages": messages,
            "temperature": temperature,
        }
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = tool_choice
        if max_tokens:
            kwargs["max_tokens"] = max_tokens

        t0 = time.time()
        completion = self._client.chat.completions.create(**kwargs)
        latency_ms = int((time.time() - t0) * 1000)

        msg = completion.choices[0].message
        usage = completion.usage

        # Strip reasoning blocks from the user-facing content. The full raw
        # content is still preserved for traces (raw_message below).
        clean_content = strip_think(msg.content)

        # Normalize tool calls into plain dicts for easier downstream handling
        tool_calls = []
        if msg.tool_calls:
            for tc in msg.tool_calls:
                tool_calls.append({
                    "id": tc.id,
                    "name": tc.function.name,
                    "arguments": tc.function.arguments,  # JSON string from MiniMax
                })

        # Cost calculation
        rate = PRICING.get(model, {"input": 0.30, "output": 1.20})
        cost = (
            usage.prompt_tokens * rate["input"] / 1_000_000
            + usage.completion_tokens * rate["output"] / 1_000_000
        )

        # Reconstruct assistant message for history. We persist the CLEAN
        # content (think blocks stripped) — keeping reasoning in history would
        # poison subsequent turns and burn tokens for nothing.
        raw_message = {"role": "assistant"}
        if clean_content:
            raw_message["content"] = clean_content
        if msg.tool_calls:
            raw_message["tool_calls"] = [
                {
                    "id": tc.id,
                    "type": "function",
                    "function": {
                        "name": tc.function.name,
                        "arguments": tc.function.arguments,
                    },
                }
                for tc in msg.tool_calls
            ]

        return LLMResponse(
            content=clean_content,
            tool_calls=tool_calls,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            latency_ms=latency_ms,
            cost_usd=cost,
            raw_message=raw_message,
        )


# Module-level singleton — callers import `client` directly.
_client: MiniMaxClient | None = None


def get_client() -> MiniMaxClient:
    global _client
    if _client is None:
        _client = MiniMaxClient()
    return _client
