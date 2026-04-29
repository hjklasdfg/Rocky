"""Web search specialist — real-time info via Brave Search."""

from __future__ import annotations

from agents._context import build_context
from agents.base import BaseAgent
from prompts.loader import render
from tools import registry, schemas


class WebAgent(BaseAgent):
    name = "web"
    max_iterations = 3  # Web search is one round-trip; cap tightly.

    def system_prompt(self, memory: dict | None = None) -> str:
        return render("web", context=build_context(memory))

    @property
    def tools(self) -> list[dict]:
        return [schemas.WEB_SEARCH]

    @property
    def tool_registry(self) -> dict:
        return {"web_search": registry.web_search}
