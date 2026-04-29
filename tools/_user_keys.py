"""Per-request user API keys via contextvars.

Mirrors the existing tools/_credentials.py pattern, but for service API keys
(MiniMax, Brave) instead of Google OAuth credentials. Each /api/chat request
sets these at the start; downstream LLM clients and tool wrappers read from
contextvars first and fall back to env vars when no per-user key is set
(legacy single-user / dev mode).

Why three vars not one:
  - minimax_chat_key  : Token Plan keys (sk-cp-) cover M2.7 chat cheaply.
  - minimax_payg_key  : Pay-as-you-go (sk-api-) covers T2A + embeddings
                        (Token Plan doesn't cover speech/embedding).
  - brave_key         : Brave Search has its own subscription model.
"""

from __future__ import annotations

import contextvars
import os

current_minimax_chat_key = contextvars.ContextVar[str | None](
    "current_minimax_chat_key", default=None,
)
current_minimax_payg_key = contextvars.ContextVar[str | None](
    "current_minimax_payg_key", default=None,
)
current_brave_key = contextvars.ContextVar[str | None](
    "current_brave_key", default=None,
)


def resolve_minimax_chat_key() -> str | None:
    """Per-user chat key, falling back to MINIMAX_API_KEY env."""
    return current_minimax_chat_key.get() or os.getenv("MINIMAX_API_KEY")


def resolve_minimax_payg_key() -> str | None:
    """Per-user pay-as-you-go key (T2A + embed), falling back through:
    MINIMAX_T2A_API_KEY → MINIMAX_API_KEY env."""
    return (
        current_minimax_payg_key.get()
        or os.getenv("MINIMAX_T2A_API_KEY")
        or os.getenv("MINIMAX_API_KEY")
    )


def resolve_brave_key() -> str | None:
    """Per-user Brave key, falling back to BRAVE_API_KEY env."""
    return current_brave_key.get() or os.getenv("BRAVE_API_KEY")


def clear_all() -> None:
    """Reset all key contextvars to None — defensive cleanup if needed."""
    current_minimax_chat_key.set(None)
    current_minimax_payg_key.set(None)
    current_brave_key.set(None)
