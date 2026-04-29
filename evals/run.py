"""Eval suite runner — execute all test cases and report a scorecard.

Usage:
    python -m evals.run                      # all cases
    python -m evals.run --kind route_only    # only routing checks (fast, cheap)
    python -m evals.run --kind end_to_end    # only full agent runs
    python -m evals.run --case e2e-web-weather

Two case kinds:

  route_only  — invoke the Router agent, assert it picks the expected route.
                Fast (heuristics often skip the LLM), cheap. Used to verify
                routing accuracy in isolation when iterating on heuristics
                or the router prompt.

  end_to_end  — run the full orchestrator: router → specialist → tools.
                Asserts route, tool calls, and reply text content. Expensive
                (real LLM + Brave + memory IO). Used to verify the entire
                pipeline behaves correctly under prompt / model changes.

Cases that need real Google credentials (email/calendar e2e) are intentionally
absent — those need OAuth + a populated inbox to run meaningfully.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

# Load env BEFORE importing any of our modules that read it.
from dotenv import load_dotenv

load_dotenv()

# Ensure we can import from project root when run as `python -m evals.run`
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.orchestrator import process as orchestrate  # noqa: E402
from agents.router import decide as route_decide  # noqa: E402
from memory import load_memory  # noqa: E402
from tracing.tracer import current_trace, end_trace, start_trace  # noqa: E402

CASES_PATH = Path(__file__).parent / "test_cases.json"


@dataclass
class CaseResult:
    case_id: str
    kind: str
    input: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    actual_route: str | None = None
    expected_route: str | None = None
    actual_tool_calls: list[str] = field(default_factory=list)
    expected_tool_calls: list[str] = field(default_factory=list)
    reply: str | None = None
    latency_ms: int = 0
    cost_usd: float = 0.0
    tokens: int = 0


def _extract_tool_calls(trace) -> list[str]:
    """Pull every tool name invoked during this trace, in order."""
    if not trace:
        return []
    names: list[str] = []
    for span in trace.spans:
        if span.kind != "tool":
            continue
        # span.name like "email.tool.search_emails" or "t2a.synthesize"
        parts = span.name.split(".")
        if len(parts) >= 3 and parts[1] == "tool":
            names.append(parts[2])
        elif len(parts) >= 2 and parts[0] in {"web", "memory", "email", "calendar", "knowledge"}:
            # legacy span shape — keep for safety
            names.append(parts[-1])
    return names


def run_route_only(case: dict) -> CaseResult:
    res = CaseResult(
        case_id=case["id"],
        kind="route_only",
        input=case["input"],
        passed=False,
        expected_route=case.get("expect_route"),
    )
    t0 = time.time()
    trace = start_trace(user_message=case["input"], user_id=None)
    try:
        memory = load_memory(None)
        decision = route_decide(case["input"], memory=memory)
        res.actual_route = decision.route
        res.cost_usd = trace.total_cost_usd
        res.tokens = trace.total_tokens
    except Exception as e:
        res.failures.append(f"router crashed: {e}")
        end_trace(reply=f"[err] {e}")
        res.latency_ms = int((time.time() - t0) * 1000)
        return res
    finally:
        end_trace()
    res.latency_ms = int((time.time() - t0) * 1000)

    expected = case.get("expect_route")
    expected_any = case.get("expect_route_any")
    if expected_any:
        if decision.route in expected_any:
            res.passed = True
        else:
            res.failures.append(f"route={decision.route!r} not in {expected_any}")
    elif expected:
        if decision.route == expected:
            res.passed = True
        else:
            res.failures.append(f"route={decision.route!r}, expected {expected!r}")
    return res


def run_end_to_end(case: dict) -> CaseResult:
    res = CaseResult(
        case_id=case["id"],
        kind="end_to_end",
        input=case["input"],
        passed=False,
        expected_route=case.get("expect_route"),
        expected_tool_calls=case.get("expect_tool_calls", []),
    )
    t0 = time.time()
    trace = start_trace(user_message=case["input"], user_id=None)
    history: list[dict] = []
    reply: str | None = None
    try:
        reply = orchestrate(case["input"], history, user_id=None)
    except Exception as e:
        res.failures.append(f"orchestrator crashed: {e}")
        end_trace(reply=f"[err] {e}")
        res.latency_ms = int((time.time() - t0) * 1000)
        return res

    end_trace(reply=reply)
    res.latency_ms = int((time.time() - t0) * 1000)
    res.reply = reply
    res.actual_route = trace.route
    res.cost_usd = trace.total_cost_usd
    res.tokens = trace.total_tokens
    res.actual_tool_calls = _extract_tool_calls(trace)

    # --- Assertions ---
    failures: list[str] = []

    expected = case.get("expect_route")
    if expected and trace.route != expected:
        failures.append(f"route={trace.route!r}, expected {expected!r}")

    expected_calls = set(case.get("expect_tool_calls", []))
    if expected_calls:
        missing = expected_calls - set(res.actual_tool_calls)
        if missing:
            failures.append(f"missing tool calls: {sorted(missing)}")

    expected_contains = case.get("expect_text_contains") or []
    reply_lower = (reply or "").lower()
    for needle in expected_contains:
        if needle.lower() not in reply_lower:
            failures.append(f"reply missing keyword: {needle!r}")

    max_cost = case.get("max_cost_usd")
    if max_cost is not None and res.cost_usd > max_cost:
        failures.append(f"cost={res.cost_usd:.6f} > budget={max_cost}")

    res.failures = failures
    res.passed = not failures
    return res


def run_case(case: dict) -> CaseResult:
    if case.get("kind") == "end_to_end":
        return run_end_to_end(case)
    return run_route_only(case)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--kind", choices=["route_only", "end_to_end"],
                        help="Filter to one case kind")
    parser.add_argument("--case", help="Run exactly one case by id")
    parser.add_argument("--json", action="store_true",
                        help="Emit machine-readable JSON instead of text")
    args = parser.parse_args()

    cases = json.loads(CASES_PATH.read_text())
    if args.kind:
        cases = [c for c in cases if c.get("kind") == args.kind]
    if args.case:
        cases = [c for c in cases if c["id"] == args.case]
    if not cases:
        print("No cases match filter.")
        sys.exit(1)

    results: list[CaseResult] = []
    for case in cases:
        if not args.json:
            print(f"  running {case['id']:30} ", end="", flush=True)
        r = run_case(case)
        results.append(r)
        if not args.json:
            mark = "✅" if r.passed else "❌"
            extra = f"  ({r.latency_ms:>5}ms, ${r.cost_usd:.5f})"
            print(f"{mark}{extra}")
            if r.failures:
                for f in r.failures:
                    print(f"     └─ {f}")

    if args.json:
        print(json.dumps([r.__dict__ for r in results], indent=2, default=str))
        return

    # Summary
    passed = sum(1 for r in results if r.passed)
    by_kind: dict[str, dict] = {}
    for r in results:
        b = by_kind.setdefault(r.kind, {"total": 0, "passed": 0, "cost": 0.0, "ms_sum": 0})
        b["total"] += 1
        b["passed"] += int(r.passed)
        b["cost"] += r.cost_usd
        b["ms_sum"] += r.latency_ms

    total_cost = sum(r.cost_usd for r in results)
    total_tokens = sum(r.tokens for r in results)
    avg_ms = int(sum(r.latency_ms for r in results) / max(1, len(results)))

    print()
    print("=" * 60)
    print(f"  Eval scorecard — {passed}/{len(results)} passed ({passed * 100 / max(1,len(results)):.0f}%)")
    print("=" * 60)
    for kind, b in by_kind.items():
        avg = int(b["ms_sum"] / max(1, b["total"]))
        print(f"  {kind:12} {b['passed']}/{b['total']:<3}  avg {avg:>5}ms  cost ${b['cost']:.5f}")
    print(f"  {'overall':12} {passed}/{len(results):<3}  avg {avg_ms:>5}ms  cost ${total_cost:.5f}  tokens {total_tokens:,}")
    print()

    if passed != len(results):
        sys.exit(2)


if __name__ == "__main__":
    main()
