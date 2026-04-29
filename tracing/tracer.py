"""Per-request agent execution tracer.

Each user message gets one Trace. Inside the trace, every LLM call,
tool call, and routing decision is recorded as a Span. Traces are
kept in memory (last N) and exposed via /trace/{id}.

Why no OpenTelemetry: the demo runs single-process; an in-memory
ring buffer is plenty. If we later need persistence, swap the storage
backend without changing the tracer interface.
"""

from __future__ import annotations

import contextvars
import time
import uuid
from collections import deque
from dataclasses import dataclass, field
from typing import Any

# Bounded buffer — older traces age out. Tune for demo: 200 traces ~= last hour.
_MAX_TRACES = 200
_traces: deque = deque(maxlen=_MAX_TRACES)
_traces_by_id: dict[str, "Trace"] = {}

# Current trace for the active request — set at request entry, read by agents.
_current_trace: contextvars.ContextVar = contextvars.ContextVar(
    "_current_trace", default=None
)


@dataclass
class Span:
    """A single sub-operation within a trace."""
    name: str                          # "router.decide", "email_agent.run", "llm.chat", etc.
    kind: str                          # "agent", "llm", "tool", "rag"
    started_at: float                  # epoch seconds
    ended_at: float | None = None
    attributes: dict = field(default_factory=dict)  # e.g. {"model": "M2", "tokens": 234}
    error: str | None = None

    @property
    def duration_ms(self) -> int:
        if self.ended_at is None:
            return 0
        return int((self.ended_at - self.started_at) * 1000)

    def end(self, **attrs):
        self.ended_at = time.time()
        self.attributes.update(attrs)

    def fail(self, error: str):
        self.ended_at = time.time()
        self.error = error


@dataclass
class Trace:
    """One end-to-end agent execution."""
    trace_id: str
    user_id: str | None
    user_message: str
    started_at: float
    ended_at: float | None = None
    spans: list[Span] = field(default_factory=list)
    final_reply: str | None = None
    total_cost_usd: float = 0.0
    total_tokens: int = 0
    route: str | None = None  # which specialist(s) handled it

    def add_span(self, name: str, kind: str, **attrs) -> Span:
        span = Span(
            name=name,
            kind=kind,
            started_at=time.time(),
            attributes=attrs,
        )
        self.spans.append(span)
        return span

    @property
    def duration_ms(self) -> int:
        end = self.ended_at or time.time()
        return int((end - self.started_at) * 1000)

    def to_dict(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id,
            "user_id": self.user_id,
            "user_message": self.user_message,
            "started_at": self.started_at,
            "duration_ms": self.duration_ms,
            "route": self.route,
            "final_reply": self.final_reply,
            "total_cost_usd": round(self.total_cost_usd, 6),
            "total_tokens": self.total_tokens,
            "spans": [
                {
                    "name": s.name,
                    "kind": s.kind,
                    "duration_ms": s.duration_ms,
                    "attributes": s.attributes,
                    "error": s.error,
                }
                for s in self.spans
            ],
        }


def start_trace(user_message: str, user_id: str | None) -> Trace:
    """Begin a new trace and set it as current for this request."""
    trace = Trace(
        trace_id=uuid.uuid4().hex[:12],
        user_id=user_id,
        user_message=user_message,
        started_at=time.time(),
    )
    _current_trace.set(trace)
    _traces.append(trace)
    _traces_by_id[trace.trace_id] = trace
    return trace


def end_trace(reply: str | None = None):
    """Close the active trace."""
    trace = _current_trace.get()
    if trace is None:
        return
    trace.ended_at = time.time()
    if reply is not None:
        trace.final_reply = reply


def current_trace() -> Trace | None:
    return _current_trace.get()


def get_trace(trace_id: str) -> Trace | None:
    return _traces_by_id.get(trace_id)


def list_traces(limit: int = 50) -> list[dict]:
    """Most recent traces first."""
    return [t.to_dict() for t in list(_traces)[-limit:][::-1]]
