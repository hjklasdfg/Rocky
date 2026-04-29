"""Router agent — decides which specialist handles a user message.

Cheap, fast, no tools. Returns a structured route + rationale that the
orchestrator dispatches on. We use temperature 0 and force JSON output so
the result is parseable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from agents._context import build_context
from llm.minimax import get_client
from prompts.loader import render
from tracing.tracer import current_trace

VALID_ROUTES = {"email", "calendar", "web", "memory", "knowledge", "smalltalk", "vision"}


@dataclass
class RouteDecision:
    route: str
    rationale: str


# Heuristic shortcuts — when we're 99% sure, skip the LLM call entirely.
# Saves tokens AND latency on the most common requests. This is the
# "推理调优加速" angle: not every request needs a routing LLM call.
#
# Order matters — first match wins. More specific patterns come first so
# e.g. "find any email from last month" routes to knowledge, not email.
_HEURISTIC_PATTERNS = [
    # Knowledge first: vague references to past emails outrank generic email.
    (r"\b(last (week|month|year)|past emails?|earlier emails?|"
     r"find any emails?|search (my )?emails? for|any emails? (about|mentioning))\b", "knowledge"),
    # Memory: explicit save/forget verbs.
    (r"\b(remember (that|this)|forget (my|the|that)|save (this|that)|note that)\b", "memory"),
    # Calendar: includes reminder phrasing (becomes a calendar event).
    (r"\b(calendar|schedule|meeting|appointment|remind me|on (my|the) calendar|"
     r"add (an? )?event|cancel (the |my )?(meeting|event))\b", "calendar"),
    # Web search: real-time info patterns.
    (r"\b(weather|news|stock|price|how (long|far|much)|score|store hours|"
     r"opening hours|what time does)\b", "web"),
    # Email last (most generic).
    (r"\b(emails?|inbox|gmail|reply|archive|unread|read (my|the) (mail|email))\b", "email"),
]


def _heuristic_route(message: str) -> str | None:
    msg = message.lower()
    for pattern, route in _HEURISTIC_PATTERNS:
        if re.search(pattern, msg):
            return route
    return None


def decide(user_message: str, memory: dict | None = None) -> RouteDecision:
    """Pick a route for the user's message.

    Tries heuristics first (zero-cost). Falls back to a tiny LLM call when
    the message is ambiguous.
    """
    trace = current_trace()
    span = trace.add_span("router.decide", "agent") if trace else None

    # Fast path: heuristic match
    fast = _heuristic_route(user_message)
    if fast:
        if span:
            span.end(route=fast, method="heuristic")
        if trace:
            trace.route = fast
        return RouteDecision(route=fast, rationale="heuristic match")

    # Slow path: ask the LLM
    context = build_context(memory)
    system = render("router", context=context)
    messages = [
        {"role": "system", "content": system},
        {"role": "user", "content": user_message},
    ]

    client = get_client()
    try:
        # Router needs reasoning room (M2 thinks) + final JSON.
        response = client.chat(
            messages=messages,
            temperature=0.0,
            max_tokens=300,
        )
    except Exception as e:
        if span:
            span.fail(str(e))
        if trace:
            trace.route = "smalltalk"
        return RouteDecision(route="smalltalk", rationale=f"router error: {e}")

    if trace:
        trace.total_cost_usd += response.cost_usd
        trace.total_tokens += response.prompt_tokens + response.completion_tokens

    decision = _parse_route(response.content or "")
    if span:
        span.end(
            route=decision.route,
            method="llm",
            model=response.model,
            tokens=response.prompt_tokens + response.completion_tokens,
            cost_usd=response.cost_usd,
        )
    if trace:
        trace.route = decision.route
    return decision


def _parse_route(text: str) -> RouteDecision:
    """Extract the JSON route from the LLM response, with fallbacks."""
    text = text.strip()
    # Strip markdown fences if the model added them despite instructions
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text, flags=re.MULTILINE).strip()

    try:
        obj = json.loads(text)
        route = obj.get("route", "smalltalk").lower().strip()
        rationale = obj.get("rationale", "")
    except json.JSONDecodeError:
        # Last-ditch regex grab
        m = re.search(r'"route"\s*:\s*"(\w+)"', text)
        route = m.group(1).lower() if m else "smalltalk"
        rationale = "fallback parse"

    if route not in VALID_ROUTES:
        route = "smalltalk"
    return RouteDecision(route=route, rationale=rationale)
