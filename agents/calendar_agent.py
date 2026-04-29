"""Calendar specialist — Google Calendar read/create/modify/delete."""

from __future__ import annotations

from agents._context import build_context
from agents.base import BaseAgent
from prompts.loader import render
from tools import registry, schemas


class CalendarAgent(BaseAgent):
    name = "calendar"

    def system_prompt(self, memory: dict | None = None) -> str:
        return render("calendar", context=build_context(memory))

    @property
    def tools(self) -> list[dict]:
        return [
            schemas.READ_CALENDAR,
            schemas.CREATE_EVENT,
            schemas.MODIFY_EVENT,
            schemas.DELETE_EVENT,
            schemas.LIST_CALENDARS,
        ]

    @property
    def tool_registry(self) -> dict:
        return {
            "read_calendar": registry.read_calendar,
            "create_event": registry.create_event,
            "modify_event": registry.modify_event,
            "delete_event": registry.delete_event,
            "list_calendars": registry.list_calendars,
        }
