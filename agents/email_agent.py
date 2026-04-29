"""Email specialist — Gmail read/search/send/archive."""

from __future__ import annotations

from agents._context import build_context
from agents.base import BaseAgent
from prompts.loader import render
from tools import registry, schemas


class EmailAgent(BaseAgent):
    name = "email"

    def system_prompt(self, memory: dict | None = None) -> str:
        return render("email", context=build_context(memory))

    @property
    def tools(self) -> list[dict]:
        return [
            schemas.READ_EMAILS,
            schemas.SEARCH_EMAILS,
            schemas.GET_FULL_EMAIL,
            schemas.SEND_EMAIL,
            schemas.ARCHIVE_EMAIL,
        ]

    @property
    def tool_registry(self) -> dict:
        return {
            "read_emails": registry.read_emails,
            "search_emails": registry.search_emails,
            "get_full_email": registry.get_full_email,
            "send_email": registry.send_email,
            "archive_email": registry.archive_email,
        }
