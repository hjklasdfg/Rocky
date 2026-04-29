"""MiniMax Text-to-Audio (T2A v2) client — speech-02 series.

Used by the chat endpoint to optionally return an audio_url alongside the
text reply, so the iOS Shortcut can play Rocky's actual synthesized voice
instead of relying on the iPhone's built-in Siri TTS.

Endpoint: https://api.minimax.io/v1/t2a_v2  (international)
Docs: https://platform.minimax.io/docs/api-reference/speech-t2a-http

Pricing: speech-02-hd is ~$0.008–$0.02 per minute of audio (varies by tier).
For voice agent use, average reply ≈ 5s of audio → ~$0.0007–$0.0017 per turn.
"""

from __future__ import annotations

import binascii
import os
import re
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

import requests


# --- Markdown stripping (voice safety) ---
#
# LLMs love to format list-style replies with **bold** and `1.` prefixes, but
# T2A reads punctuation literally — "asterisk asterisk Ningqian Yang asterisk
# asterisk" is brutal. We strip the most common Markdown noise before sending
# to T2A. This is a defense-in-depth backstop; the prompts also tell agents
# not to emit Markdown.
_MD_BOLD = re.compile(r"\*\*(.+?)\*\*", re.DOTALL)
_MD_ITAL = re.compile(r"(?<!\*)\*(?!\s)(.+?)(?<!\s)\*(?!\*)", re.DOTALL)
_MD_CODE = re.compile(r"`([^`]+)`")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]+\)")
_MD_HEADING = re.compile(r"^#{1,6}\s+", re.MULTILINE)
_MD_BULLET = re.compile(r"^\s*[-*+]\s+", re.MULTILINE)


def _strip_markdown_for_speech(text: str) -> str:
    """Remove Markdown punctuation that TTS would pronounce literally.

    Keeps numbered list prefixes ("1. ") because TTS reads them naturally as
    "one, two, three". Strips bold/italic/code/link/heading/bullet markers.
    """
    text = _MD_BOLD.sub(r"\1", text)
    text = _MD_ITAL.sub(r"\1", text)
    text = _MD_CODE.sub(r"\1", text)
    text = _MD_LINK.sub(r"\1", text)
    text = _MD_HEADING.sub("", text)
    text = _MD_BULLET.sub("", text)
    return text

# Default voice when MINIMAX_VOICE_ID isn't set or for legacy clients.
# "English_expressive_narrator" is a built-in MiniMax preset — warm, friendly,
# good for assistant-style replies. Override with your own cloned voice_id later.
DEFAULT_VOICE = os.getenv("MINIMAX_VOICE_ID", "English_expressive_narrator")
DEFAULT_MODEL = os.getenv("MINIMAX_T2A_MODEL", "speech-02-hd")

# Audio cache — generated files stored here, served via /audio/{filename} in main.py.
AUDIO_DIR = Path(os.getenv("AUDIO_CACHE_DIR", "./audio_cache"))
AUDIO_DIR.mkdir(parents=True, exist_ok=True)

# How long to keep generated files before cleanup. The iOS Shortcut fetches
# the audio immediately after /api/chat returns, so a short TTL is fine.
AUDIO_TTL_SECONDS = 60 * 60  # 1 hour

DEFAULT_TIMEOUT = 30  # seconds — voice synthesis can be slower than chat


@dataclass
class T2AResult:
    """Output of synthesize() — file path + serving URL + cost metadata."""
    filename: str            # e.g. "abc123.mp3" — relative to AUDIO_DIR
    file_path: Path
    duration_ms: int         # synthesis latency
    audio_bytes: int         # size of the generated mp3
    voice_id: str
    model: str


class T2AError(Exception):
    pass


def _endpoint() -> str:
    base = os.getenv("MINIMAX_BASE_URL", "https://api.minimax.io/v1").rstrip("/")
    return f"{base}/t2a_v2"


def _api_key() -> str:
    """Prefer a dedicated T2A key (pay-as-you-go) over the main key.

    MiniMax sells text models on Token Plans (subscription) but charges T2A
    on pay-as-you-go — so users typically have two keys: sk-cp-* for chat,
    sk-api-* for speech. Set MINIMAX_T2A_API_KEY in .env to use them
    independently. Falls back to MINIMAX_API_KEY when not configured.
    """
    key = os.getenv("MINIMAX_T2A_API_KEY") or os.getenv("MINIMAX_API_KEY")
    if not key:
        raise T2AError("MINIMAX_API_KEY (or MINIMAX_T2A_API_KEY) not set.")
    return key


def synthesize(
    text: str,
    *,
    voice_id: str | None = None,
    model: str | None = None,
    speed: float = 1.0,
    fmt: str = "mp3",
    sample_rate: int = 32000,
    bitrate: int = 128000,
) -> T2AResult:
    """Generate audio for `text` and persist it to AUDIO_DIR.

    Returns a T2AResult with the local filename — caller composes the
    public URL (e.g. f"/audio/{result.filename}").
    """
    if not text.strip():
        raise T2AError("Cannot synthesize empty text.")

    # Strip Markdown so T2A doesn't read "asterisk asterisk" out loud.
    text = _strip_markdown_for_speech(text)

    voice_id = voice_id or DEFAULT_VOICE
    model = model or DEFAULT_MODEL

    payload = {
        "model": model,
        "text": text,
        "stream": False,
        "voice_setting": {
            "voice_id": voice_id,
            "speed": speed,
            "vol": 1.0,
            "pitch": 0,
        },
        "audio_setting": {
            "sample_rate": sample_rate,
            "bitrate": bitrate,
            "format": fmt,
            "channel": 1,
        },
        # Helps the model pronounce non-English snippets in mixed-language input.
        "language_boost": "auto",
    }
    headers = {
        "Authorization": f"Bearer {_api_key()}",
        "Content-Type": "application/json",
    }

    t0 = time.time()
    resp = requests.post(_endpoint(), json=payload, headers=headers, timeout=DEFAULT_TIMEOUT)
    duration_ms = int((time.time() - t0) * 1000)

    try:
        resp.raise_for_status()
    except requests.HTTPError as e:
        raise T2AError(f"T2A HTTP {resp.status_code}: {resp.text[:300]}") from e

    body = resp.json()
    # MiniMax returns either:
    #   data.audio  → hex-encoded audio bytes (most common for /t2a_v2)
    #   data.audio_url  → CDN URL (sometimes, depending on plan)
    data = body.get("data") or {}
    base_resp = body.get("base_resp") or {}
    if base_resp.get("status_code") and base_resp["status_code"] != 0:
        raise T2AError(f"T2A error: {base_resp.get('status_msg', 'unknown')}")

    hex_audio = data.get("audio")
    audio_url = data.get("audio_url")

    if hex_audio:
        try:
            audio_bytes = binascii.unhexlify(hex_audio)
        except (binascii.Error, ValueError) as e:
            raise T2AError(f"Bad hex audio in response: {e}") from e
    elif audio_url:
        # CDN path — fetch and persist locally so all callers serve from one URL.
        cdn = requests.get(audio_url, timeout=DEFAULT_TIMEOUT)
        cdn.raise_for_status()
        audio_bytes = cdn.content
    else:
        raise T2AError(f"No audio in response: keys={list(data.keys())}")

    filename = f"{uuid.uuid4().hex[:12]}.{fmt}"
    file_path = AUDIO_DIR / filename
    file_path.write_bytes(audio_bytes)

    return T2AResult(
        filename=filename,
        file_path=file_path,
        duration_ms=duration_ms,
        audio_bytes=len(audio_bytes),
        voice_id=voice_id,
        model=model,
    )


def cleanup_old() -> int:
    """Best-effort GC of audio files older than AUDIO_TTL_SECONDS.

    Called opportunistically by main.py before each chat turn — keeps the
    cache from growing unbounded without needing a real scheduler.
    """
    cutoff = time.time() - AUDIO_TTL_SECONDS
    removed = 0
    try:
        for f in AUDIO_DIR.iterdir():
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink(missing_ok=True)
                removed += 1
    except OSError:
        pass
    return removed
