"""One-shot RAG backfill / re-index script.

Use this when you need to (re-)build the email vector store outside the OAuth
signup flow — e.g. after changing the backfill query, after first-login backfill
silently failed, or before a demo where you want fresh vectors.

Usage:
    python -m scripts.rag_backfill                       # uses USER_EMAIL env or first user in DB
    python -m scripts.rag_backfill --email you@x.com     # specific user
    python -m scripts.rag_backfill --query "in:inbox newer_than:1y"
    python -m scripts.rag_backfill --max 1000

Run from project root with the venv active so module imports resolve.
"""
from __future__ import annotations

import argparse
import os
import sys

# Load .env BEFORE any project imports — several modules read env at import time.
from dotenv import load_dotenv
load_dotenv()

from auth import get_credentials_for_user
from database import _get_conn  # noqa: lightweight reuse for one ad-hoc query
from rag.email_indexer import DEFAULT_BACKFILL_QUERY, MAX_BACKFILL_MESSAGES, backfill


def _find_user(email: str | None) -> dict:
    """Look up a user by email, or fall back to the only user in the DB."""
    conn = _get_conn()
    try:
        with conn.cursor() as cur:
            if email:
                cur.execute("SELECT id, email, name FROM users WHERE email = %s", (email,))
                row = cur.fetchone()
                if not row:
                    sys.exit(f"[rag_backfill] No user with email {email!r} in database.")
                return dict(row)
            cur.execute("SELECT id, email, name FROM users ORDER BY created_at LIMIT 2")
            rows = cur.fetchall()
            if not rows:
                sys.exit("[rag_backfill] No users in database. Sign up via /login first.")
            if len(rows) > 1:
                sys.exit(
                    "[rag_backfill] Multiple users in DB — specify --email "
                    f"(found: {[r['email'] for r in rows]})"
                )
            return dict(rows[0])
    finally:
        conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Re-index a user's Gmail history into the RAG store.")
    parser.add_argument("--email", default=os.getenv("USER_EMAIL"), help="Target user's email (default: $USER_EMAIL or sole user)")
    parser.add_argument("--query", default=DEFAULT_BACKFILL_QUERY, help=f"Gmail search query (default: {DEFAULT_BACKFILL_QUERY!r})")
    parser.add_argument("--max", type=int, default=MAX_BACKFILL_MESSAGES, help=f"Max messages to index (default: {MAX_BACKFILL_MESSAGES})")
    args = parser.parse_args()

    user = _find_user(args.email)
    print(f"[rag_backfill] User: {user['email']} (id={user['id']})")
    print(f"[rag_backfill] Query: {args.query!r}  max={args.max}")

    creds = get_credentials_for_user(user["id"])
    result = backfill(
        user_id=user["id"],
        credentials=creds,
        query=args.query,
        max_messages=args.max,
    )
    print(f"[rag_backfill] Done. {result}")


if __name__ == "__main__":
    main()
