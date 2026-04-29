"""Knowledge specialist — semantic RAG over the user's email history."""

from __future__ import annotations

from agents._context import build_context
from agents.base import BaseAgent
from prompts.loader import render
from tools import registry, schemas


class KnowledgeAgent(BaseAgent):
    name = "knowledge"
    max_iterations = 4

    def system_prompt(self, memory: dict | None = None) -> str:
        return render("knowledge", context=build_context(memory))

    @property
    def tools(self) -> list[dict]:
        return [schemas.SEARCH_EMAIL_HISTORY]

    @property
    def tool_registry(self) -> dict:
        return {"search_email_history": registry.search_email_history}
