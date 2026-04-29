"""Brave Search API wrapper — replaces Gemini's Google Search Grounding.

Two modes:
- `search()`: standard Web Search API. Returns title/url/snippet.
- `summarize()`: Brave's AI Summarizer endpoint. Returns a synthesized answer
   with citations — closer to Gemini Grounding in shape.

Auth: BRAVE_API_KEY env var (get one at https://brave.com/search/api/).
Free tier: 2,000 queries/month, 1 query/sec.
"""

from __future__ import annotations

import os

import requests

WEB_ENDPOINT = "https://api.search.brave.com/res/v1/web/search"
SUMMARIZER_ENDPOINT = "https://api.search.brave.com/res/v1/summarizer/search"

DEFAULT_TIMEOUT = 8  # seconds — voice agent needs fast responses


class BraveSearchError(Exception):
    pass


def _headers() -> dict:
    # Per-user key (contextvar set in /api/chat) takes precedence over env.
    from tools._user_keys import resolve_brave_key
    api_key = resolve_brave_key()
    if not api_key:
        raise BraveSearchError(
            "No Brave key (set BRAVE_API_KEY in .env or configure per-user "
            "via /settings; get one at https://brave.com/search/api/)."
        )
    return {
        "Accept": "application/json",
        "X-Subscription-Token": api_key,
    }


def search(query: str, count: int = 5) -> dict:
    """Standard web search. Returns top results.

    Args:
        query: Search query.
        count: Number of results (max 20).

    Returns:
        {"status": "success", "results": [{"title", "url", "description"}, ...]}
        or {"status": "error", "message": "..."}.
    """
    try:
        resp = requests.get(
            WEB_ENDPOINT,
            headers=_headers(),
            params={"q": query, "count": min(count, 20)},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
    except requests.RequestException as e:
        return {"status": "error", "message": f"Brave search failed: {e}"}

    data = resp.json()
    web_results = (data.get("web") or {}).get("results") or []
    return {
        "status": "success",
        "results": [
            {
                "title": r.get("title", ""),
                "url": r.get("url", ""),
                "description": r.get("description", ""),
            }
            for r in web_results[:count]
        ],
    }


def summarize(query: str, count: int = 5) -> dict:
    """AI-grounded summary with citations.

    Two-step protocol per Brave docs:
      1. Hit /web/search with summary=1 to get a `summarizer.key`
      2. Hit /summarizer/search with that key to get the summary

    Falls back to plain `search()` if the summarizer plan tier isn't available.

    Returns:
        {"status": "success", "answer": "...", "citations": [...]}.
    """
    try:
        # Step 1: web search with summary flag
        resp = requests.get(
            WEB_ENDPOINT,
            headers=_headers(),
            params={"q": query, "summary": 1, "count": count},
            timeout=DEFAULT_TIMEOUT,
        )
        resp.raise_for_status()
        data = resp.json()
        summarizer_key = (data.get("summarizer") or {}).get("key")

        if not summarizer_key:
            # No summarizer available (free tier or unsupported query) — fall back
            return _format_search_as_answer(data, query)

        # Step 2: fetch the actual summary
        sum_resp = requests.get(
            SUMMARIZER_ENDPOINT,
            headers=_headers(),
            params={"key": summarizer_key, "entity_info": 1},
            timeout=DEFAULT_TIMEOUT,
        )
        sum_resp.raise_for_status()
        sum_data = sum_resp.json()

        summary_text = ""
        for msg in sum_data.get("summary", []):
            if msg.get("type") == "token":
                summary_text += msg.get("data", "")

        if not summary_text.strip():
            return _format_search_as_answer(data, query)

        return {
            "status": "success",
            "answer": summary_text.strip(),
            "citations": _extract_citations(data),
        }
    except requests.RequestException as e:
        return {"status": "error", "message": f"Brave search failed: {e}"}


def _format_search_as_answer(data: dict, query: str) -> dict:
    """When summarizer isn't available, synthesize a brief answer from the
    top results so the agent can still respond.
    """
    results = (data.get("web") or {}).get("results") or []
    if not results:
        return {"status": "success", "answer": f"No results for '{query}'.", "citations": []}

    bullets = []
    citations = []
    for r in results[:3]:
        bullets.append(f"- {r.get('title', '')}: {r.get('description', '')}")
        citations.append({"title": r.get("title", ""), "url": r.get("url", "")})

    answer = "Top results:\n" + "\n".join(bullets)
    return {"status": "success", "answer": answer, "citations": citations}


def _extract_citations(data: dict) -> list[dict]:
    results = (data.get("web") or {}).get("results") or []
    return [
        {"title": r.get("title", ""), "url": r.get("url", "")}
        for r in results[:5]
    ]
