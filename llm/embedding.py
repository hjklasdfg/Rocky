"""MiniMax embeddings — used by the RAG email index.

MiniMax's embedding endpoint is NOT OpenAI-compatible (different request shape),
so we hit it directly via HTTP. Returns 1024-dim vectors for embo-02.

Reference: https://www.minimaxi.com/document/guides/embeddings
"""

from __future__ import annotations

import os
import time

import requests

DEFAULT_MODEL = os.getenv("MINIMAX_EMBED_MODEL", "embo-02")
ENDPOINT = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/") + "/embeddings"

# embo-02 returns 1024-dim vectors. Used to size the Chroma collection.
EMBED_DIM = {"embo-01": 1536, "embo-02": 1024}

# Retry policy for transient MiniMax errors. RPM rate-limit (status_code 1002)
# is the common one to hit during bulk backfill on Token Plan keys.
_RETRYABLE_CODES = {1002, 1004, 1027, 2049}  # rate limit / server busy / overloaded
_MAX_RETRIES = 5


def embed(
    texts: list[str],
    *,
    model: str | None = None,
    purpose: str = "db",  # "db" for indexing, "query" for retrieval
) -> list[list[float]]:
    """Embed a batch of texts via MiniMax. Returns list of vectors.

    Retries on transient errors (RPM rate-limit, server busy) with exponential
    backoff. Raises with the MiniMax error message on permanent failures.
    """
    model = model or DEFAULT_MODEL
    # Token Plan keys (sk-cp-) typically have very tight RPM caps on embeddings.
    # Allow a separate pay-as-you-go key (sk-api-) for embeddings — falling back
    # to the T2A key (which is already pay-as-you-go for users who needed voice),
    # and finally to the chat key for users whose plan does cover embeddings.
    api_key = (
        os.getenv("MINIMAX_EMBED_API_KEY")
        or os.getenv("MINIMAX_T2A_API_KEY")
        or os.getenv("MINIMAX_API_KEY")
    )
    if not api_key:
        raise RuntimeError("No MiniMax API key set (MINIMAX_EMBED_API_KEY / MINIMAX_T2A_API_KEY / MINIMAX_API_KEY).")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "texts": texts,
        "type": purpose,
    }

    last_err: str | None = None
    for attempt in range(_MAX_RETRIES):
        resp = requests.post(ENDPOINT, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        vectors = data.get("vectors")
        if vectors is not None:
            return vectors

        # vectors is null — inspect base_resp to decide retry vs raise.
        base = data.get("base_resp") or {}
        code = base.get("status_code")
        msg = base.get("status_msg", "unknown error")
        last_err = f"code={code} msg={msg!r}"

        if code in _RETRYABLE_CODES and attempt < _MAX_RETRIES - 1:
            wait = 2 ** attempt  # 1, 2, 4, 8, 16 seconds
            print(f"[embed] {last_err} — sleeping {wait}s (retry {attempt + 1}/{_MAX_RETRIES})")
            time.sleep(wait)
            continue

        raise RuntimeError(f"MiniMax embed failed ({last_err}); full response: {data}")

    raise RuntimeError(f"MiniMax embed: exhausted {_MAX_RETRIES} retries ({last_err})")


def embed_one(text: str, **kwargs) -> list[float]:
    return embed([text], **kwargs)[0]
