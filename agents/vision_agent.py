"""Vision specialist — multimodal image + voice input.

Activated automatically by the orchestrator when the incoming /api/chat
request carries an `image_base64` field. The agent runs on a vision-capable
MiniMax model (M2.7-VL by default) and has access to the cross-cutting tool
set so it can take action across calendar / email / memory / knowledge / web
in the same turn — no separate handoff.

Why a single agent (not separate vision-routes-to-specialist)?
  Vision-driven turns usually have one obvious downstream action: "add to
  calendar", "save contact", "email this". Round-tripping through a second
  specialist after parsing the image would double the cost and latency for
  no quality gain. The vision agent owns the whole turn end-to-end.
"""

from __future__ import annotations

import os

from agents._context import build_context
from agents.base import BaseAgent
from prompts.loader import render
from tools import registry, schemas


class VisionAgent(BaseAgent):
    name = "vision"

    @property
    def model(self) -> str | None:
        # MiniMax-VL-01 is the vision-language model on the international
        # platform — it's a separate model line from the M2.x text family
        # (M2.7 is text-only despite what naming might suggest). Override
        # via env if MiniMax ships a successor (MiniMax-VL-02 etc.).
        return os.getenv("MINIMAX_VISION_MODEL", "MiniMax-VL-01")

    def system_prompt(self, memory: dict | None = None) -> str:
        return render("vision", context=build_context(memory))

    @property
    def tools(self) -> list[dict]:
        # The full cross-domain toolkit. The vision agent might add a calendar
        # event, save a contact, send an email, or look up past correspondence
        # — sometimes within a single turn — so we expose all of them.
        return [
            schemas.CREATE_EVENT,
            schemas.READ_CALENDAR,
            schemas.SEND_EMAIL,
            schemas.SAVE_MEMORY,
            schemas.SEARCH_EMAIL_HISTORY,
            schemas.WEB_SEARCH,
        ]

    @property
    def tool_registry(self) -> dict:
        return {
            "create_event": registry.create_event,
            "read_calendar": registry.read_calendar,
            "send_email": registry.send_email,
            "save_memory": registry.save_memory,
            "search_email_history": registry.search_email_history,
            "web_search": registry.web_search,
        }
