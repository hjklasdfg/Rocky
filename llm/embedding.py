"""Embedding provider abstraction — pluggable vector backends for RAG.

Three providers, switchable via the EMBEDDING_PROVIDER env var:

  - 'local'    : sentence-transformers, runs on Mac mini / laptop with no
                 API key, no quota, no network. Default. Uses MPS on Apple
                 Silicon, CUDA on Nvidia, else CPU.
  - 'minimax'  : MiniMax embo-02 via HTTP. Currently not publicly exposed
                 on the international platform (api.minimax.io) — kept for
                 users with China-platform accounts or whitelist access.
  - 'openai'   : OpenAI text-embedding-3-small. Cheap ($0.02/1M tokens)
                 and reliable. Requires OPENAI_API_KEY.

Why an abstraction at all?
  RAG semantics don't depend on whose model produced the vectors — what
  matters is that index and query go through the same provider so cosine
  distances are meaningful. Decoupling the provider lets us swap backends
  for cost, privacy, or vendor-lock-in reasons without touching agents
  or the email_store.
"""

from __future__ import annotations

import os
import time
from functools import lru_cache

# --- Config ---

PROVIDER = os.getenv("EMBEDDING_PROVIDER", "local").lower()

# Per-provider default models. Override per-provider via *_EMBED_MODEL env.
LOCAL_MODEL = os.getenv("LOCAL_EMBED_MODEL", "all-MiniLM-L6-v2")
MINIMAX_MODEL = os.getenv("MINIMAX_EMBED_MODEL", "embo-02")
OPENAI_MODEL = os.getenv("OPENAI_EMBED_MODEL", "text-embedding-3-small")

# Vector dimensions per known model — used by callers that need to size
# matrices upfront. Exposed as a module constant rather than a function
# because it's small and static.
EMBED_DIM = {
    # MiniMax
    "embo-01": 1536,
    "embo-02": 1024,
    # sentence-transformers (Hugging Face model names)
    "all-MiniLM-L6-v2": 384,
    "all-mpnet-base-v2": 768,
    "BAAI/bge-small-en-v1.5": 384,
    "BAAI/bge-base-en-v1.5": 768,
    # OpenAI
    "text-embedding-3-small": 1536,
    "text-embedding-3-large": 3072,
    "text-embedding-ada-002": 1536,
}


# --- MiniMax provider (preserved for users with China-platform access) ---

MINIMAX_ENDPOINT = (
    os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/") + "/embeddings"
)
_RETRYABLE_CODES = {1002, 1004, 1027, 2049}  # rate limit / server busy / overloaded
_MAX_RETRIES = 6
_RATE_LIMIT_BASE_WAIT = 30  # seconds — RPM windows are 60s wide
_OTHER_BASE_WAIT = 2


def _embed_minimax(texts: list[str], purpose: str = "db") -> list[list[float]]:
    import requests

    # Resolution: dedicated embed env > per-user payg key (contextvar) > T2A env > main env.
    # Per-user payg covers embeddings same way it covers T2A — both are
    # pay-as-you-go on MiniMax's billing model.
    from tools._user_keys import resolve_minimax_payg_key
    api_key = (
        os.getenv("MINIMAX_EMBED_API_KEY")
        or resolve_minimax_payg_key()
    )
    if not api_key:
        raise RuntimeError(
            "No MiniMax key for embeddings (MINIMAX_EMBED_API_KEY / "
            "MINIMAX_T2A_API_KEY / MINIMAX_API_KEY env, or per-user "
            "minimax_payg key via /settings)."
        )

    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}
    payload = {"model": MINIMAX_MODEL, "texts": texts, "type": purpose}

    last_err: str | None = None
    for attempt in range(_MAX_RETRIES):
        resp = requests.post(MINIMAX_ENDPOINT, json=payload, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        vectors = data.get("vectors")
        if vectors is not None:
            return vectors

        base = data.get("base_resp") or {}
        code = base.get("status_code")
        msg = base.get("status_msg", "unknown error")
        last_err = f"code={code} msg={msg!r}"

        if code in _RETRYABLE_CODES and attempt < _MAX_RETRIES - 1:
            wait = (
                _RATE_LIMIT_BASE_WAIT + 15 * attempt
                if code == 1002
                else _OTHER_BASE_WAIT * (2 ** attempt)
            )
            print(f"[embed:minimax] {last_err} — sleeping {wait}s (retry {attempt + 1}/{_MAX_RETRIES})")
            time.sleep(wait)
            continue

        raise RuntimeError(f"MiniMax embed failed ({last_err}); full response: {data}")

    raise RuntimeError(f"MiniMax embed: exhausted {_MAX_RETRIES} retries ({last_err})")


# --- Local provider (sentence-transformers, runs on-device) ---


@lru_cache(maxsize=1)
def _local_model():
    """Load and cache the sentence-transformer model.

    Cold start is ~3-8s (first call only — downloads weights ≈ 22-110MB to
    ~/.cache/torch/sentence_transformers, then loads into RAM). Subsequent
    calls are sub-second per batch.
    """
    from sentence_transformers import SentenceTransformer

    # Auto-detect best device. MPS on Apple Silicon Mac mini is ~3x CPU speed.
    device = "cpu"
    try:
        import torch  # type: ignore

        if torch.backends.mps.is_available():
            device = "mps"
        elif torch.cuda.is_available():
            device = "cuda"
    except ImportError:
        pass

    print(f"[embed:local] loading {LOCAL_MODEL!r} on {device} (one-time, may download model weights)")
    model = SentenceTransformer(LOCAL_MODEL, device=device)
    print(
        f"[embed:local] ready (dim={model.get_sentence_embedding_dimension()}, device={device})"
    )
    return model


def _embed_local(texts: list[str], purpose: str = "db") -> list[list[float]]:
    """Embed via sentence-transformers locally. No network, no quota."""
    model = _local_model()
    # normalize_embeddings=True so a downstream dot-product == cosine similarity.
    vectors = model.encode(
        texts,
        normalize_embeddings=True,
        show_progress_bar=False,
        batch_size=32,
    )
    return vectors.tolist()


# --- OpenAI provider ---


def _embed_openai(texts: list[str], purpose: str = "db") -> list[list[float]]:
    """Embed via OpenAI text-embedding-3-small.

    Pricing: $0.02 per 1M input tokens. 500 emails ≈ 250K tokens ≈ $0.005.
    """
    from openai import OpenAI

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("OPENAI_API_KEY not set.")
    client = OpenAI(api_key=api_key)
    resp = client.embeddings.create(model=OPENAI_MODEL, input=texts)
    return [d.embedding for d in resp.data]


# --- Public dispatch ---


def embed(
    texts: list[str],
    *,
    model: str | None = None,  # kept for backward compat, unused — provider config wins
    purpose: str = "db",
) -> list[list[float]]:
    """Embed a batch of texts via the configured provider.

    Returns a list of float vectors with the provider's native dimensionality.
    All providers normalize vectors so that dot-product == cosine similarity.
    """
    if PROVIDER == "local":
        return _embed_local(texts, purpose)
    if PROVIDER == "minimax":
        return _embed_minimax(texts, purpose)
    if PROVIDER == "openai":
        return _embed_openai(texts, purpose)
    raise RuntimeError(
        f"Unknown EMBEDDING_PROVIDER: {PROVIDER!r} (expected 'local' / 'minimax' / 'openai')"
    )


def embed_one(text: str, **kwargs) -> list[float]:
    return embed([text], **kwargs)[0]


def current_provider_info() -> dict:
    """Surface the active provider config for /metrics or admin endpoints."""
    if PROVIDER == "local":
        return {"provider": "local", "model": LOCAL_MODEL}
    if PROVIDER == "minimax":
        return {"provider": "minimax", "model": MINIMAX_MODEL}
    if PROVIDER == "openai":
        return {"provider": "openai", "model": OPENAI_MODEL}
    return {"provider": PROVIDER, "model": None}
