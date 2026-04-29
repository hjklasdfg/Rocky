"""Bootstrap and incremental indexing of the user's Gmail history into the RAG store.

Called from main.py:
- After OAuth login → full backfill (last 6 months) in a background task.
- Periodically or on-demand → incremental top-up of new emails.

Kept dependency-light: only Gmail API + email_store. No agent code.
"""

from __future__ import annotations

import base64
import re
from email.utils import parsedate_to_datetime

from googleapiclient.discovery import build

from rag.email_store import collection_size, upsert_emails

DEFAULT_BACKFILL_QUERY = "in:inbox newer_than:6m"
BACKFILL_PAGE_SIZE = 50
MAX_BACKFILL_MESSAGES = 500  # Cap to keep MiniMax embed cost bounded for demos


def _decode_body(payload: dict) -> str:
    """Best-effort plain text extraction from a Gmail message payload."""
    if payload.get("mimeType") == "text/plain":
        data = (payload.get("body") or {}).get("data")
        if data:
            return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")

    for part in payload.get("parts") or []:
        text = _decode_body(part)
        if text:
            return text

    # HTML fallback
    if payload.get("mimeType") == "text/html":
        data = (payload.get("body") or {}).get("data")
        if data:
            html = base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
            return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", html)).strip()
    return ""


def _extract(msg: dict) -> dict:
    """Flatten a Gmail message into the dict shape email_store wants."""
    headers = {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}
    body = _decode_body(msg.get("payload", {}))
    return {
        "message_id": msg["id"],
        "from": headers.get("From", ""),
        "subject": headers.get("Subject", ""),
        "date": headers.get("Date", ""),
        "body": body[:2000],  # Truncate to keep embed payloads tight
    }


def backfill(
    user_id: str | None,
    credentials,
    query: str = DEFAULT_BACKFILL_QUERY,
    max_messages: int = MAX_BACKFILL_MESSAGES,
) -> dict:
    """One-shot index of recent history. Idempotent — upsert by message_id."""
    service = build("gmail", "v1", credentials=credentials)

    indexed = 0
    page_token = None
    fetched = 0

    while fetched < max_messages:
        page = service.users().messages().list(
            userId="me",
            q=query,
            maxResults=min(BACKFILL_PAGE_SIZE, max_messages - fetched),
            pageToken=page_token,
        ).execute()
        ids = [m["id"] for m in page.get("messages", [])]
        if not ids:
            break

        batch_emails = []
        for mid in ids:
            try:
                msg = service.users().messages().get(
                    userId="me", id=mid, format="full"
                ).execute()
                batch_emails.append(_extract(msg))
            except Exception as e:
                print(f"[indexer] Skipped {mid}: {e}")

        indexed += upsert_emails(batch_emails, user_id=user_id)
        fetched += len(ids)

        page_token = page.get("nextPageToken")
        if not page_token:
            break

    return {
        "indexed": indexed,
        "scanned": fetched,
        "collection_size": collection_size(user_id),
    }
