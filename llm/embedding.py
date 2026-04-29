"""MiniMax embeddings — used by the RAG email index.

MiniMax's embedding endpoint is NOT OpenAI-compatible (different request shape),
so we hit it directly via HTTP. Returns 1024-dim vectors for embo-02.

Reference: https://www.minimaxi.com/document/guides/embeddings
"""

from __future__ import annotations

import os

import requests

DEFAULT_MODEL = os.getenv("MINIMAX_EMBED_MODEL", "embo-02")
ENDPOINT = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/") + "/embeddings"

# embo-02 returns 1024-dim vectors. Used to size the Chroma collection.
EMBED_DIM = {"embo-01": 1536, "embo-02": 1024}


def embed(
    texts: list[str],
    *,
    model: str | None = None,
    purpose: str = "db",  # "db" for indexing, "query" for retrieval
) -> list[list[float]]:
    """Embed a batch of texts via MiniMax. Returns list of vectors."""
    model = model or DEFAULT_MODEL
    api_key = os.getenv("MINIMAX_API_KEY")
    if not api_key:
        raise RuntimeError("MINIMAX_API_KEY not set.")

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    payload = {
        "model": model,
        "texts": texts,
        "type": purpose,
    }
    resp = requests.post(ENDPOINT, json=payload, headers=headers, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "vectors" not in data:
        raise RuntimeError(f"Unexpected MiniMax embed response: {data}")
    return data["vectors"]


def embed_one(text: str, **kwargs) -> list[float]:
    return embed([text], **kwargs)[0]
