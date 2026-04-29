"""Memory specialist — explicit save / forget commands only."""

from __future__ import annotations

from agents._context import build_context
from agents.base import BaseAgent
from prompts.loader import render
from tools import registry, schemas


class MemoryAgent(BaseAgent):
    name = "memory"
    max_iterations = 3

    def system_prompt(self, memory: dict | None = None) -> str:
        return render("memory", context=build_context(memory))

    @property
    def tools(self) -> list[dict]:
        return [schemas.SAVE_MEMORY, schemas.DELETE_MEMORY]

    @property
    def tool_registry(self) -> dict:
        return {
            "save_memory": registry.save_memory,
            "delete_memory": registry.delete_memory,
        }
