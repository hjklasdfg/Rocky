"""Tool callable wrappers — bridge the existing Gmail/Calendar/Memory/Brave
modules to the agent loop with credentials threaded via contextvars.

Each function here matches a schema in tools/schemas.py and returns a JSON-
serializable dict.
"""

from __future__ import annotations

from memory import delete_fact, save_fact
from tools._credentials import current_credentials, current_user_id
from tools.brave_search import summarize as brave_summarize
from tools.calendar_tools import (
    create_event as _create_event,
    delete_event as _delete_event,
    list_calendars as _list_calendars,
    modify_event as _modify_event,
    read_calendar as _read_calendar,
)
from tools.gmail_tools import (
    archive_email as _archive_email,
    get_full_email as _get_full_email,
    read_emails as _read_emails,
    search_emails as _search_emails,
    send_email as _send_email,
)


# --- Gmail ---

def read_emails(max_results: int = 10) -> dict:
    return _read_emails(
        max_results=max_results,
        credentials=current_credentials.get(),
        user_id=current_user_id.get(),
    )


def search_emails(query: str, max_results: int = 10) -> dict:
    return _search_emails(
        query=query, max_results=max_results, credentials=current_credentials.get()
    )


def get_full_email(message_id: str) -> dict:
    return _get_full_email(message_id=message_id, credentials=current_credentials.get())


def send_email(to: str, subject: str, body: str, reply_to_message_id: str = "") -> dict:
    return _send_email(
        to=to,
        subject=subject,
        body=body,
        reply_to_message_id=reply_to_message_id or None,
        credentials=current_credentials.get(),
    )


def archive_email(message_id: str) -> dict:
    return _archive_email(message_id=message_id, credentials=current_credentials.get())


# --- Calendar ---

def read_calendar(date: str = "", calendar_id: str = "all") -> dict:
    return _read_calendar(
        date=date or None, calendar_id=calendar_id, credentials=current_credentials.get()
    )


def create_event(
    summary: str,
    date: str,
    start_time: str,
    end_time: str = "",
    description: str = "",
    reminder_minutes: int = 10,
    calendar_id: str = "primary",
) -> dict:
    return _create_event(
        summary=summary,
        date=date,
        start_time=start_time,
        end_time=end_time or None,
        description=description,
        reminder_minutes=reminder_minutes,
        calendar_id=calendar_id,
        credentials=current_credentials.get(),
    )


def delete_event(event_id: str, calendar_id: str = "primary") -> dict:
    return _delete_event(
        event_id=event_id, calendar_id=calendar_id, credentials=current_credentials.get()
    )


def modify_event(
    event_id: str,
    summary: str = "",
    date: str = "",
    start_time: str = "",
    end_time: str = "",
    description: str = "",
    calendar_id: str = "primary",
) -> dict:
    return _modify_event(
        event_id=event_id,
        summary=summary or None,
        date=date or None,
        start_time=start_time or None,
        end_time=end_time or None,
        description=description or None,
        calendar_id=calendar_id,
        credentials=current_credentials.get(),
    )


def list_calendars(**_: object) -> dict:
    # Accept and ignore any kwargs — the OpenAI tool schema has a dummy
    # `_unused` parameter (MiniMax requires non-empty `properties`), but the
    # underlying Calendar API call takes nothing.
    return _list_calendars(credentials=current_credentials.get())


# --- Memory ---

def save_memory(fact: str) -> dict:
    return save_fact(fact, user_id=current_user_id.get())


def delete_memory(fact_keyword: str) -> dict:
    return delete_fact(fact_keyword, user_id=current_user_id.get())


# --- Web search ---

def web_search(query: str) -> dict:
    return brave_summarize(query)


# --- RAG (deferred — wired in by knowledge_agent once email_store is built) ---

def search_email_history(query: str, k: int = 5) -> dict:
    """Late-bound to avoid circular import — knowledge_agent imports this lazily."""
    from rag.email_store import search as _rag_search
    return _rag_search(
        query=query, k=k, user_id=current_user_id.get(), credentials=current_credentials.get()
    )
