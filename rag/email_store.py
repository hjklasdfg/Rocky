"""Email RAG store — semantic search over the user's Gmail history.

Storage: a tiny numpy-based vector store with one .npz + .json file per user
on local disk. Chosen over Chroma/FAISS for two reasons:
  - At demo scale (≤2K emails per user), a flat cosine search over a numpy
    matrix is sub-millisecond — no need for HNSW.
  - Zero compile dependencies — works on any Python without Rust toolchain.

Layout:
  STORE_DIR/
    {user_id}.npz   — vectors (N × dim float32)
    {user_id}.json  — parallel metadata: ids, from, subject, date, snippets

If you ever outgrow this (>50K emails), swap the backend behind the same
`search()` / `upsert_emails()` interface.
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path

import numpy as np

from llm.embedding import embed, embed_one
from tracing.tracer import current_trace

STORE_DIR = Path(os.getenv("RAG_STORE_PATH", "./rag_store"))
STORE_DIR.mkdir(parents=True, exist_ok=True)

# One lock per user prevents racing upsert + search.
_locks: dict[str, threading.Lock] = {}


def _user_key(user_id: str | None) -> str:
    return user_id or "legacy"


def _lock_for(user_id: str | None) -> threading.Lock:
    key = _user_key(user_id)
    if key not in _locks:
        _locks[key] = threading.Lock()
    return _locks[key]


def _paths(user_id: str | None) -> tuple[Path, Path]:
    key = _user_key(user_id)
    return STORE_DIR / f"{key}.npz", STORE_DIR / f"{key}.json"


def _load(user_id: str | None) -> tuple[np.ndarray | None, list[dict]]:
    npz_path, json_path = _paths(user_id)
    if not npz_path.exists() or not json_path.exists():
        return None, []
    vectors = np.load(npz_path)["vectors"]
    metas = json.loads(json_path.read_text())
    return vectors, metas


def _save(user_id: str | None, vectors: np.ndarray, metas: list[dict]) -> None:
    npz_path, json_path = _paths(user_id)
    np.savez(npz_path, vectors=vectors.astype(np.float32))
    json_path.write_text(json.dumps(metas, ensure_ascii=False))


def _normalize(matrix: np.ndarray) -> np.ndarray:
    """L2-normalize so dot product == cosine similarity."""
    norms = np.linalg.norm(matrix, axis=1, keepdims=True)
    norms[norms == 0] = 1.0
    return matrix / norms


def search(
    query: str,
    *,
    k: int = 5,
    user_id: str | None = None,
    credentials=None,  # Unused at read time — kept for tool signature symmetry.
) -> dict:
    """Semantic search over the user's indexed email history."""
    trace = current_trace()
    span = trace.add_span("rag.search", "rag", k=k) if trace else None

    with _lock_for(user_id):
        vectors, metas = _load(user_id)

    if vectors is None or len(metas) == 0:
        if span:
            span.end(empty=True)
        return {
            "status": "empty",
            "message": "Email history not yet indexed. Try again in a minute.",
            "results": [],
        }

    try:
        q = np.asarray(embed_one(query, purpose="query"), dtype=np.float32)
    except Exception as e:
        if span:
            span.fail(f"embed query: {e}")
        return {"status": "error", "message": str(e), "results": []}

    q_norm = q / (np.linalg.norm(q) or 1.0)
    matrix = _normalize(vectors)
    scores = matrix @ q_norm  # cosine similarity, shape (N,)
    top_idx = np.argsort(-scores)[: min(k, len(scores))]

    hits = []
    for i in top_idx:
        meta = metas[int(i)]
        hits.append({
            "message_id": meta.get("message_id", ""),
            "from": meta.get("from", ""),
            "subject": meta.get("subject", ""),
            "date": meta.get("date", ""),
            "snippet": meta.get("snippet", "")[:200],
            "score": round(float(scores[int(i)]), 3),
        })

    if span:
        span.end(hits=len(hits), top_score=hits[0]["score"] if hits else 0.0)

    return {"status": "success", "results": hits}


def upsert_emails(emails: list[dict], user_id: str | None = None) -> int:
    """Add or update emails in the index. Idempotent on message_id.

    Each email dict must have: message_id, from, subject, date, body.
    """
    if not emails:
        return 0

    docs, metas_new = [], []
    for e in emails:
        body = e.get("body") or e.get("snippet") or ""
        text = f"{e.get('subject', '')}\n\n{body}".strip()
        if not text or not e.get("message_id"):
            continue
        docs.append(text)
        metas_new.append({
            "message_id": e["message_id"],
            "from": e.get("from", ""),
            "subject": e.get("subject", ""),
            "date": e.get("date", ""),
            "snippet": text[:300],
        })

    if not docs:
        return 0

    BATCH = 32
    new_vecs: list[list[float]] = []
    for i in range(0, len(docs), BATCH):
        new_vecs.extend(embed(docs[i:i + BATCH], purpose="db"))
    new_matrix = np.asarray(new_vecs, dtype=np.float32)

    with _lock_for(user_id):
        existing_vectors, existing_metas = _load(user_id)

        # Build id → index map from existing metas for upsert semantics.
        index_by_id = {m["message_id"]: idx for idx, m in enumerate(existing_metas)}

        if existing_vectors is None:
            merged_vectors = new_matrix
            merged_metas = list(metas_new)
        else:
            merged_vectors = existing_vectors.copy()
            merged_metas = list(existing_metas)
            for vec, meta in zip(new_matrix, metas_new):
                mid = meta["message_id"]
                if mid in index_by_id:
                    idx = index_by_id[mid]
                    merged_vectors[idx] = vec
                    merged_metas[idx] = meta
                else:
                    merged_vectors = np.vstack([merged_vectors, vec[None, :]])
                    merged_metas.append(meta)
                    index_by_id[mid] = len(merged_metas) - 1

        _save(user_id, merged_vectors, merged_metas)

    return len(docs)


def collection_size(user_id: str | None = None) -> int:
    with _lock_for(user_id):
        _, metas = _load(user_id)
    return len(metas)
