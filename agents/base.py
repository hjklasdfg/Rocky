"""Agent base class — unified LLM call, tracing, and tool execution.

Every specialist agent inherits from this. Subclasses define:
- `name`: identifier shown in traces ("email", "calendar", etc.)
- `system_prompt(memory)`: returns the system prompt for this turn
- `tools`: list of OpenAI-format tool schemas
- `tool_registry`: dict mapping tool name -> Python callable

The base class handles the agent loop, tool dispatch, history management,
and emits trace spans for every LLM call and tool invocation.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod

from llm.minimax import LLMResponse, get_client
from tracing.tracer import current_trace


def _sanitize_history(history: list[dict]) -> list[dict]:
    """Drop malformed messages before sending to MiniMax.

    MiniMax (especially M2.7 with tools attached) returns error 2013
    "invalid chat setting" when any message in the conversation has:
    - empty/whitespace-only content
    - missing `content` key
    - non-string content
    - dangling `tool_calls` without paired tool responses
    - role outside user/assistant

    Session history *should* only ever contain clean text turns (we only
    write text via base.py and orchestrator), but older sessions or future
    bugs can sneak malformed entries in. Once a bad message lands in
    history, every subsequent specialist turn fails with 2013. This filter
    is defense-in-depth — applied to all specialist agents via run(),
    mirroring the same filter on orchestrator's smalltalk path.
    """
    cleaned = []
    for m in history:
        if m.get("role") not in ("user", "assistant"):
            continue
        content = m.get("content")
        if not isinstance(content, str) or not content.strip():
            continue
        if m.get("tool_calls"):
            continue
        cleaned.append({"role": m["role"], "content": content})
    return cleaned


class BaseAgent(ABC):
    """Abstract base for all specialist agents.

    Subclasses must implement `system_prompt` and define `name`, `tools`,
    `tool_registry`. The `run` method is the main entry point.
    """

    name: str = "base"
    max_iterations: int = 6  # Safety cap on the agent loop

    @abstractmethod
    def system_prompt(self, memory: dict | None = None) -> str:
        """Return the system prompt for this agent.

        Memory is passed so prompts can include user context (name, contacts,
        facts) without each subclass having to load it itself.
        """
        ...

    @property
    def tools(self) -> list[dict]:
        """OpenAI-format tool schemas. Empty by default — override in subclasses."""
        return []

    @property
    def tool_registry(self) -> dict:
        """Map tool name -> Python callable. Override in subclasses."""
        return {}

    @property
    def model(self) -> str | None:
        """Override to use a different MiniMax model for this agent.

        Returning None falls back to llm.minimax.DEFAULT_MODEL. Use this to
        route cheap decisions (the Router) to a smaller model.
        """
        return None

    def _execute_tool(self, name: str, arguments_json: str) -> dict:
        """Look up and run a tool. Always returns a JSON-serializable dict."""
        trace = current_trace()
        span = trace.add_span(f"{self.name}.tool.{name}", "tool") if trace else None

        try:
            args = json.loads(arguments_json) if arguments_json else {}
        except json.JSONDecodeError as e:
            if span:
                span.fail(f"bad arguments JSON: {e}")
            return {"error": f"Could not parse arguments: {e}"}

        fn = self.tool_registry.get(name)
        if fn is None:
            if span:
                span.fail(f"unknown tool: {name}")
            return {"error": f"Unknown tool: {name}"}

        try:
            result = fn(**args)
        except Exception as e:
            if span:
                span.fail(str(e))
            return {"error": str(e)}

        if span:
            span.end(result_size=len(json.dumps(result, default=str)))
        return result

    def _llm_call(self, messages: list[dict], **kwargs) -> LLMResponse:
        """Single LLM call with tracing + cost accounting."""
        trace = current_trace()
        span = trace.add_span(f"{self.name}.llm", "llm") if trace else None

        client = get_client()
        model = kwargs.pop("model", None) or self.model
        try:
            response = client.chat(
                messages=messages,
                tools=self.tools or None,
                model=model,
                **kwargs,
            )
        except Exception as e:
            if span:
                span.fail(str(e))
            raise

        if span:
            span.end(
                model=response.model,
                tokens=response.prompt_tokens + response.completion_tokens,
                prompt_tokens=response.prompt_tokens,
                completion_tokens=response.completion_tokens,
                cost_usd=response.cost_usd,
                latency_ms=response.latency_ms,
            )

        if trace:
            trace.total_cost_usd += response.cost_usd
            trace.total_tokens += response.prompt_tokens + response.completion_tokens

        return response

    def run(
        self,
        user_message: str,
        history: list[dict],
        memory: dict | None = None,
    ) -> str:
        """Execute the agent loop and return final text reply.

        history is mutated in place — the agent appends its messages to it.
        """
        trace = current_trace()
        agent_span = trace.add_span(f"{self.name}.run", "agent") if trace else None

        messages = [
            {"role": "system", "content": self.system_prompt(memory)},
            *_sanitize_history(history),
            {"role": "user", "content": user_message},
        ]

        for iteration in range(self.max_iterations):
            response = self._llm_call(messages)

            # Append assistant message (may contain content + tool_calls)
            if response.raw_message:
                messages.append(response.raw_message)

            if not response.has_tool_calls:
                # Final answer
                reply = response.content or ""
                # Sync history (skip system, keep the user/assistant exchange).
                # IMPORTANT: never write a malformed assistant message into
                # history. MiniMax rejects assistant messages that are empty
                # or have no `content` key with error 2013 ("invalid chat
                # setting"), and that error then poisons every subsequent
                # turn in the same session. Always write a non-empty content.
                history.append({"role": "user", "content": user_message})
                history.append({
                    "role": "assistant",
                    "content": reply or "(no reply)",
                })
                if agent_span:
                    agent_span.end(iterations=iteration + 1, reply_chars=len(reply))
                return reply

            # Execute all tool calls and append results
            for tc in response.tool_calls:
                result = self._execute_tool(tc["name"], tc["arguments"])
                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": json.dumps(result, default=str, ensure_ascii=False),
                })

        # Hit iteration cap
        fallback = "I'm having trouble completing that. Could you rephrase?"
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": fallback})
        if agent_span:
            agent_span.end(iterations=self.max_iterations, hit_cap=True)
        return fallback
