"""Per-request credential threading via contextvars.

Set at the start of each /api/chat call by main.py; read by tool wrappers
so we don't have to thread credentials through every agent and tool call.

This is identical to the original Gemini-version pattern — preserved
intentionally so the Gmail/Calendar tool modules need zero changes.
"""

from __future__ import annotations

import contextvars

current_credentials = contextvars.ContextVar("current_credentials", default=None)
current_user_id = contextvars.ContextVar("current_user_id", default=None)
