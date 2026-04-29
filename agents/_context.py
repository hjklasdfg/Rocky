"""Build the {context} block injected into every specialist's system prompt.

Centralizes date/time + user memory rendering so prompts don't drift.
"""

from __future__ import annotations

from datetime import datetime


def build_context(memory: dict | None) -> str:
    """Render user memory + current time into the prompt context block."""
    now = datetime.now()
    lines = [
        f"Current date: {now.strftime('%A, %B %d, %Y')}",
        f"Current time: {now.strftime('%I:%M %p')}",
    ]

    if not memory:
        lines.append("User memory: empty (new user).")
        return "\n".join(lines)

    if memory.get("user_name"):
        lines.append(f"User's name: {memory['user_name']}")
    if memory.get("user_email"):
        lines.append(f"User's email: {memory['user_email']}")

    contacts = memory.get("contacts") or {}
    if contacts:
        # Cap at 30 to keep prompt budget sane
        items = list(contacts.items())[:30]
        contacts_str = ", ".join(f"{name} ({email})" for name, email in items)
        lines.append(f"Known contacts: {contacts_str}")

    facts = memory.get("facts") or []
    if facts:
        lines.append("Remembered facts about the user:")
        for fact in facts:
            lines.append(f"  - {fact}")

    return "\n".join(lines)
