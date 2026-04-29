"""OpenAI-compatible tool schemas for every Rocky tool.

Each schema is a dict in the OpenAI tools format:
    {"type": "function", "function": {"name", "description", "parameters"}}

We define these explicitly (instead of auto-generating from docstrings)
because OpenAI's spec is structured and we want full control over the
schema MiniMax sees. Easier to debug, easier to A/B prompt variations.
"""

from __future__ import annotations

# --- Gmail ---

READ_EMAILS = {
    "type": "function",
    "function": {
        "name": "read_emails",
        "description": (
            "Read the most recent emails from the user's Gmail INBOX (any category — "
            "Primary, Promotions, Updates, Social all included). Use for unfiltered "
            "'read my emails' / 'check my inbox' / 'latest email' requests. "
            "If the user mentions ANY filter (sender/time/topic/unread), use search_emails instead."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "max_results": {
                    "type": "integer",
                    "description": "How many emails to return. 'read my emails' → 10, 'show all today' → 20, 'latest email' → 1.",
                    "default": 10,
                },
            },
        },
    },
}

SEARCH_EMAILS = {
    "type": "function",
    "function": {
        "name": "search_emails",
        "description": (
            "Search Gmail using Gmail query syntax: from:, subject:, is:unread, "
            "newer_than:Xd, has:attachment, category:primary. Use whenever the user "
            "specifies any filter."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {
                    "type": "string",
                    "description": "Gmail query, e.g. 'from:sarah newer_than:7d category:primary'.",
                },
                "max_results": {
                    "type": "integer",
                    "default": 10,
                },
            },
            "required": ["query"],
        },
    },
}

GET_FULL_EMAIL = {
    "type": "function",
    "function": {
        "name": "get_full_email",
        "description": "Get the full untruncated body of a specific email by message ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {
                    "type": "string",
                    "description": "Message ID from a prior read_emails / search_emails result.",
                },
            },
            "required": ["message_id"],
        },
    },
}

SEND_EMAIL = {
    "type": "function",
    "function": {
        "name": "send_email",
        "description": (
            "Send a new email or reply. ALWAYS pass reply_to_message_id when replying "
            "so threading works."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "to": {"type": "string", "description": "Recipient email address."},
                "subject": {"type": "string"},
                "body": {"type": "string"},
                "reply_to_message_id": {
                    "type": "string",
                    "description": "Set when replying to an existing email. Leave empty for new emails.",
                    "default": "",
                },
            },
            "required": ["to", "subject", "body"],
        },
    },
}

ARCHIVE_EMAIL = {
    "type": "function",
    "function": {
        "name": "archive_email",
        "description": (
            "Remove an email from the inbox (NOT permanently delete — still searchable). "
            "Only call when the user explicitly says 'archive', 'remove', or 'clean up'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "message_id": {"type": "string"},
            },
            "required": ["message_id"],
        },
    },
}

# --- Calendar ---

READ_CALENDAR = {
    "type": "function",
    "function": {
        "name": "read_calendar",
        "description": (
            "Read events from the user's Google Calendar for a specific date. By "
            "default aggregates events across ALL of the user's calendars (personal, "
            "shared, holidays). Each returned event has a `calendar` field telling "
            "you which calendar it came from."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "date": {
                    "type": "string",
                    "description": "Date in YYYY-MM-DD format. Empty = today.",
                    "default": "",
                },
                "calendar_id": {
                    "type": "string",
                    "description": (
                        "'all' (default) reads every calendar the user subscribes to. "
                        "Pass 'primary' to limit to the primary calendar only, or a "
                        "specific calendar ID returned by list_calendars to target one."
                    ),
                    "default": "all",
                },
            },
        },
    },
}

CREATE_EVENT = {
    "type": "function",
    "function": {
        "name": "create_event",
        "description": "Create a calendar event with a phone notification reminder.",
        "parameters": {
            "type": "object",
            "properties": {
                "summary": {"type": "string", "description": "Event title."},
                "date": {"type": "string", "description": "YYYY-MM-DD."},
                "start_time": {"type": "string", "description": "HH:MM 24-hour."},
                "end_time": {
                    "type": "string",
                    "description": "HH:MM 24-hour. Empty = +1 hour after start.",
                    "default": "",
                },
                "description": {"type": "string", "default": ""},
                "reminder_minutes": {"type": "integer", "default": 10},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["summary", "date", "start_time"],
        },
    },
}

DELETE_EVENT = {
    "type": "function",
    "function": {
        "name": "delete_event",
        "description": "Cancel/delete a calendar event by ID.",
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
    },
}

MODIFY_EVENT = {
    "type": "function",
    "function": {
        "name": "modify_event",
        "description": (
            "Modify a calendar event. Only pass fields that need to change — "
            "duration is auto-preserved when only start_time changes."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "event_id": {"type": "string"},
                "summary": {"type": "string", "default": ""},
                "date": {"type": "string", "default": ""},
                "start_time": {"type": "string", "default": ""},
                "end_time": {"type": "string", "default": ""},
                "description": {"type": "string", "default": ""},
                "calendar_id": {"type": "string", "default": "primary"},
            },
            "required": ["event_id"],
        },
    },
}

LIST_CALENDARS = {
    "type": "function",
    "function": {
        "name": "list_calendars",
        "description": (
            "List all calendars the user has access to. Call this FIRST when the user "
            "names a specific calendar so you can find the right calendar_id."
        ),
        # NOTE: MiniMax rejects tools with empty `properties: {}` (error 2013
        # "invalid chat setting"), even though OpenAI accepts them. Provide a
        # dummy unused parameter so the schema validates. The LLM should never
        # pass anything for it; the registry wrapper ignores it either way.
        "parameters": {
            "type": "object",
            "properties": {
                "_unused": {
                    "type": "string",
                    "description": "Reserved — leave empty. This tool takes no inputs.",
                    "default": "",
                },
            },
        },
    },
}

# --- Memory ---

SAVE_MEMORY = {
    "type": "function",
    "function": {
        "name": "save_memory",
        "description": "Save a concise fact/preference to the user's long-term memory.",
        "parameters": {
            "type": "object",
            "properties": {
                "fact": {
                    "type": "string",
                    "description": "Concise canonical statement, e.g. 'Prefers morning meetings'.",
                },
            },
            "required": ["fact"],
        },
    },
}

DELETE_MEMORY = {
    "type": "function",
    "function": {
        "name": "delete_memory",
        "description": "Remove all stored facts containing this keyword.",
        "parameters": {
            "type": "object",
            "properties": {
                "fact_keyword": {"type": "string"},
            },
            "required": ["fact_keyword"],
        },
    },
}

# --- Web ---

WEB_SEARCH = {
    "type": "function",
    "function": {
        "name": "web_search",
        "description": (
            "Search the live web via Brave for real-time info: weather, news, prices, "
            "hours, sports, current events. Pass a CONCISE search query, not the user's full sentence."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
            },
            "required": ["query"],
        },
    },
}

# --- RAG ---

SEARCH_EMAIL_HISTORY = {
    "type": "function",
    "function": {
        "name": "search_email_history",
        "description": (
            "Semantic search over the user's PAST email history (last 6 months, indexed). "
            "Use for vague references to old emails: 'what did Sarah say last month'."
        ),
        "parameters": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "Natural-language semantic query."},
                "k": {"type": "integer", "default": 5, "description": "Number of results."},
            },
            "required": ["query"],
        },
    },
}
