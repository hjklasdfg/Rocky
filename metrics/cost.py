"""Aggregate cost & latency metrics across all traces.

Pulls from the tracing module — the source of truth is the Trace ring buffer.
This module just aggregates for the /metrics endpoint.
"""

from __future__ import annotations

from collections import defaultdict

from tracing.tracer import _traces


def aggregate() -> dict:
    """Roll up all in-memory traces into a metrics summary.

    Returns:
        {
          "total_requests": int,
          "total_cost_usd": float,
          "total_tokens": int,
          "avg_latency_ms": int,
          "p95_latency_ms": int,
          "by_route": {route_name: {requests, cost, avg_latency_ms}},
          "by_model": {model_name: {calls, tokens, cost}},
        }
    """
    traces = list(_traces)
    if not traces:
        return {
            "total_requests": 0,
            "total_cost_usd": 0.0,
            "total_tokens": 0,
            "avg_latency_ms": 0,
            "p95_latency_ms": 0,
            "by_route": {},
            "by_model": {},
        }

    durations = sorted(t.duration_ms for t in traces)
    total_cost = sum(t.total_cost_usd for t in traces)
    total_tokens = sum(t.total_tokens for t in traces)

    by_route: dict[str, dict] = defaultdict(
        lambda: {"requests": 0, "cost_usd": 0.0, "latency_sum_ms": 0}
    )
    by_model: dict[str, dict] = defaultdict(
        lambda: {"calls": 0, "tokens": 0, "cost_usd": 0.0}
    )

    for t in traces:
        route = t.route or "unrouted"
        by_route[route]["requests"] += 1
        by_route[route]["cost_usd"] += t.total_cost_usd
        by_route[route]["latency_sum_ms"] += t.duration_ms
        for span in t.spans:
            if span.kind == "llm":
                model = span.attributes.get("model", "unknown")
                by_model[model]["calls"] += 1
                by_model[model]["tokens"] += span.attributes.get("tokens", 0)
                by_model[model]["cost_usd"] += span.attributes.get("cost_usd", 0.0)

    # Finalize by_route averages
    by_route_out = {}
    for route, stats in by_route.items():
        n = stats["requests"]
        by_route_out[route] = {
            "requests": n,
            "cost_usd": round(stats["cost_usd"], 6),
            "avg_latency_ms": int(stats["latency_sum_ms"] / n) if n else 0,
        }

    # p95 latency
    p95_idx = max(0, int(len(durations) * 0.95) - 1)

    return {
        "total_requests": len(traces),
        "total_cost_usd": round(total_cost, 6),
        "total_tokens": total_tokens,
        "avg_latency_ms": int(sum(durations) / len(durations)),
        "p95_latency_ms": durations[p95_idx],
        "by_route": by_route_out,
        "by_model": {k: {**v, "cost_usd": round(v["cost_usd"], 6)} for k, v in by_model.items()},
    }
