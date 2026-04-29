"""Top-level orchestrator — routes a user message to the right specialist.

Replaces the original `agent.process_message()` function. The flow:

  1. Set credentials + user_id contextvars (multi-user threading).
  2. Greeting fast path — bypass LLM entirely (carried over from original).
  3. Router agent picks a route.
  4. Dispatch to the specialist; specialist runs its tool loop and returns text.
  5. History is updated in OpenAI message format.

Notes:
- Specialists share a single conversation history, so follow-ups across
  domains work ("read my emails" → email agent → "what's the weather"
  → web agent, both see the same history).
- Each specialist has its own system prompt, but the Aggregator pattern
  (combining outputs from multiple specialists in one turn) is reserved for
  Tier 3 — for now, exactly one specialist handles each turn.
"""

from __future__ import annotations

from datetime import datetime

from agents.calendar_agent import CalendarAgent
from agents.email_agent import EmailAgent
from agents.knowledge_agent import KnowledgeAgent
from agents.memory_agent import MemoryAgent
from agents.router import decide as route_decide
from agents.vision_agent import VisionAgent
from agents.web_agent import WebAgent
from memory import load_memory, save_memory
from tools._credentials import current_credentials, current_user_id
from tracing.tracer import current_trace

# Specialist instances — stateless, safe to share across requests.
_SPECIALISTS = {
    "email": EmailAgent(),
    "calendar": CalendarAgent(),
    "web": WebAgent(),
    "memory": MemoryAgent(),
    "knowledge": KnowledgeAgent(),
    "vision": VisionAgent(),
}


# --- Greeting fast path (carried from original agent.py) -----------------

GREETINGS = {
    "hi", "hey", "hello", "good morning", "good afternoon", "good evening",
    "hi rocky", "hey rocky", "hello rocky", "good morning rocky",
    "good afternoon rocky", "good evening rocky",
}


def _is_greeting(message: str) -> bool:
    return message.lower().strip().rstrip("!.,") in GREETINGS


def _fast_greeting(history: list, user_id: str | None) -> str:
    """Greeting reply built from direct API calls — zero LLM tokens.

    Carries over the original Rocky design: count unread + today's events
    via Gmail/Calendar batch APIs, build a one-line briefing.
    """
    from googleapiclient.discovery import build as build_service

    now = datetime.now()
    hour = now.hour
    if hour < 12:
        greeting = "Good morning"
    elif hour < 17:
        greeting = "Good afternoon"
    else:
        greeting = "Good evening"

    memory = load_memory(user_id)
    name_parts = (memory.get("user_name") or "").split()
    name = name_parts[0] if name_parts else "there"

    # Mid-conversation greeting → just acknowledge
    if history:
        return f"{greeting}, {name}! What can I help with?"

    parts = [f"{greeting}, {name}!"]
    creds = current_credentials.get()
    if not creds:
        try:
            from auth import get_credentials
            creds = get_credentials()
        except Exception:
            creds = None

    if creds:
        # Unread emails since last session
        try:
            gmail = build_service("gmail", "v1", credentials=creds)
            last_ts = memory.get("last_session_timestamp")
            if last_ts:
                last_dt = datetime.fromisoformat(last_ts)
                q = f"is:unread category:primary after:{last_dt.strftime('%Y/%m/%d')}"
            else:
                q = "is:unread category:primary newer_than:1d"
            unread = gmail.users().messages().list(
                userId="me", q=q, maxResults=100
            ).execute()
            count = unread.get("resultSizeEstimate", 0)
            if count > 0:
                parts.append(f"You have {count} new email{'s' if count != 1 else ''}.")
        except Exception:
            pass

        # Today's remaining events
        try:
            tz = now.astimezone().tzinfo
            cal = build_service("calendar", "v3", credentials=creds)
            events = cal.events().list(
                calendarId="primary",
                timeMin=now.replace(tzinfo=tz).isoformat(),
                timeMax=now.replace(hour=23, minute=59, second=59, tzinfo=tz).isoformat(),
                singleEvents=True,
            ).execute()
            n = len(events.get("items", []))
            if n > 0:
                parts.append(f"You have {n} event{'s' if n != 1 else ''} ahead today.")
        except Exception:
            pass

    parts.append("What would you like to do?" if len(parts) > 1 else "What can I help with?")
    return " ".join(parts)


# --- Main entry ----------------------------------------------------------

def process(
    user_message: str,
    history: list,
    user_id: str | None = None,
    *,
    image_base64: str | None = None,
    image_mime: str = "image/jpeg",
) -> str:
    """Route + dispatch one user turn. Returns the spoken-reply text.

    `history` is mutated in place — caller (SessionManager) re-reads it next turn.

    Multimodal: when `image_base64` is set, the router is short-circuited
    and the turn goes straight to the vision specialist. The vision agent
    owns the full turn (extract → call tool → reply) so we don't pay for a
    second LLM hop in another specialist.
    """
    # Wire credentials for tool calls
    if user_id:
        from auth import get_credentials_for_user
        current_credentials.set(get_credentials_for_user(user_id))
        current_user_id.set(user_id)
    else:
        current_credentials.set(None)
        current_user_id.set(None)

    # 0. Multimodal short-circuit — image present means vision turn.
    # Skip the router entirely (saves a routing LLM call) since image-bearing
    # requests always go to the vision specialist.
    if image_base64:
        trace = current_trace()
        if trace:
            trace.route = "vision"
        memory = load_memory(user_id)
        reply = _SPECIALISTS["vision"].run(
            user_message,
            history,
            memory=memory,
            image_base64=image_base64,
            image_mime=image_mime,
        )
        _maybe_compact(history)
        return reply

    # 1. Greeting fast path
    if _is_greeting(user_message):
        trace = current_trace()
        if trace:
            trace.route = "greeting_fastpath"
        reply = _fast_greeting(history, user_id)
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        return reply

    # 2. Routing
    memory = load_memory(user_id)
    decision = route_decide(user_message, memory=memory)

    # 3. Smalltalk fallback — answer with a tiny LLM turn, no tools.
    if decision.route == "smalltalk":
        from llm.minimax import get_client
        client = get_client()
        # Defensive sanitizer — old sessions from earlier code paths could
        # contain malformed assistant messages (empty content, dangling
        # tool_calls without responses) that trigger MiniMax error 2013.
        # Filter the history to only well-formed user/assistant text turns.
        clean_history = [
            m for m in history
            if m.get("role") in ("user", "assistant")
            and isinstance(m.get("content"), str)
            and m.get("content").strip()
            and not m.get("tool_calls")
        ]
        msgs = [
            {"role": "system", "content": "You are Rocky, a warm voice assistant. Reply in 1-2 sentences for voice."},
            *clean_history,
            {"role": "user", "content": user_message},
        ]
        # M2 is a reasoning model — needs headroom for <think> + final reply.
        # 400 tokens covers most chit-chat without runaway cost.
        resp = client.chat(messages=msgs, temperature=0.5, max_tokens=400)
        trace = current_trace()
        if trace:
            trace.total_cost_usd += resp.cost_usd
            trace.total_tokens += resp.prompt_tokens + resp.completion_tokens
        reply = resp.content or "I'm here. What can I help with?"
        history.append({"role": "user", "content": user_message})
        history.append({"role": "assistant", "content": reply})
        return reply

    # 4. Dispatch to specialist
    specialist = _SPECIALISTS.get(decision.route)
    if specialist is None:
        # Unknown route — degrade gracefully
        history.append({"role": "user", "content": user_message})
        fallback = "I'm not sure how to handle that. Could you rephrase?"
        history.append({"role": "assistant", "content": fallback})
        return fallback

    reply = specialist.run(user_message, history, memory=memory)

    # 5. History compaction (basic, Tier 2). Tier 3 will summarise via MiniMax.
    _maybe_compact(history)

    return reply


# --- Compaction ----------------------------------------------------------

MAX_HISTORY_MESSAGES = 24
KEEP_RECENT = 8


def _maybe_compact(history: list) -> None:
    """Trim history when it gets long. Tier 2: simple truncation.

    Tier 3 will replace this with a MiniMax summarisation call so we keep
    semantic context (IDs, names) while reducing tokens.
    """
    if len(history) <= MAX_HISTORY_MESSAGES:
        return
    recent = history[-KEEP_RECENT:]
    history.clear()
    history.append({
        "role": "system",
        "content": "[Earlier conversation truncated to save tokens.]",
    })
    history.extend(recent)


# --- Hook used by main.py on session end ---------------------------------

def remember_session_end(user_id: str | None) -> None:
    """Save the session-end timestamp so the next greeting reports new emails."""
    memory = load_memory(user_id)
    memory["last_session_timestamp"] = datetime.now().isoformat()
    save_memory(memory, user_id)
