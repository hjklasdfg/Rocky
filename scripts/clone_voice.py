"""One-shot voice cloning script for Rocky.

Workflow (per MiniMax docs https://platform.minimax.io/docs/guides/speech-voice-clone):
    1. POST /v1/files/upload with purpose="voice_clone" → file_id
    2. POST /v1/voice_clone with file_id + your custom voice_id → done
    3. Add MINIMAX_VOICE_ID=<your voice_id> to .env, restart uvicorn

Usage:
    python scripts/clone_voice.py --audio /path/to/sample.mp3 --voice-id rocky_linyi_v1

Audio requirements (source file):
    - Format: mp3, m4a, or wav
    - Duration: 10 seconds — 5 minutes (recommend 30—60s of clean speech)
    - Size: ≤ 20 MB
    - Single speaker, quiet environment, natural intonation

The script uses MINIMAX_T2A_API_KEY if set (your pay-as-you-go speech key),
falling back to MINIMAX_API_KEY. Voice cloning is billed under the speech
plan, so the T2A key is what you want.
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

import requests
from dotenv import load_dotenv


BASE_URL = "https://api.minimax.io"
UPLOAD_PATH = "/v1/files/upload"
CLONE_PATH = "/v1/voice_clone"
DEFAULT_MODEL = "speech-02-hd"  # match what the runtime T2A uses


def _api_key() -> str:
    key = os.getenv("MINIMAX_T2A_API_KEY") or os.getenv("MINIMAX_API_KEY")
    if not key:
        sys.exit("ERROR: neither MINIMAX_T2A_API_KEY nor MINIMAX_API_KEY is set in .env")
    return key


def upload_source_audio(audio_path: Path, api_key: str) -> str:
    """Upload the source audio and return the file_id."""
    if not audio_path.exists():
        sys.exit(f"ERROR: audio file not found: {audio_path}")

    suffix = audio_path.suffix.lower().lstrip(".")
    if suffix not in {"mp3", "m4a", "wav"}:
        sys.exit(f"ERROR: unsupported audio format '.{suffix}'. Use mp3, m4a, or wav.")

    size_mb = audio_path.stat().st_size / (1024 * 1024)
    if size_mb > 20:
        sys.exit(f"ERROR: file is {size_mb:.1f} MB; MiniMax max is 20 MB.")

    print(f"[1/2] Uploading {audio_path.name} ({size_mb:.2f} MB)...")
    with audio_path.open("rb") as f:
        resp = requests.post(
            BASE_URL + UPLOAD_PATH,
            headers={"Authorization": f"Bearer {api_key}"},
            data={"purpose": "voice_clone"},
            files={"file": (audio_path.name, f)},
            timeout=120,
        )
    if resp.status_code != 200:
        sys.exit(f"ERROR: upload failed [{resp.status_code}]: {resp.text}")

    file_id = resp.json().get("file", {}).get("file_id")
    if not file_id:
        sys.exit(f"ERROR: no file_id in upload response: {resp.text}")

    print(f"      ✓ uploaded — file_id={file_id}")
    return str(file_id)


def clone_voice(
    file_id: str,
    voice_id: str,
    api_key: str,
    *,
    preview_text: str | None = None,
    model: str | None = None,
) -> dict:
    """Trigger the clone. Returns the API response (may include a preview audio URL).

    Empirically, passing `model` or `text` here returns 2013 invalid_params on
    speech-02-hd accounts. The minimum-viable payload is just file_id + voice_id;
    actual T2A model choice happens later at synthesis time.
    """
    payload: dict = {
        "file_id": file_id,
        "voice_id": voice_id,
    }
    if model:
        payload["model"] = model
    if preview_text:
        payload["text"] = preview_text

    print(f"[2/2] Cloning voice as voice_id='{voice_id}'...")
    resp = requests.post(
        BASE_URL + CLONE_PATH,
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json=payload,
        timeout=180,
    )
    if resp.status_code != 200:
        sys.exit(f"ERROR: clone failed [{resp.status_code}]: {resp.text}")

    body = resp.json()
    # MiniMax sometimes returns 200 with an embedded error code in the body.
    base_resp = body.get("base_resp") or {}
    if base_resp.get("status_code", 0) not in (0, 200):
        sys.exit(f"ERROR: clone API error: {base_resp}")
    return body


def main() -> None:
    parser = argparse.ArgumentParser(description="Clone a voice via MiniMax for Rocky.")
    parser.add_argument("--audio", required=True, type=Path, help="Path to source mp3/m4a/wav")
    parser.add_argument(
        "--voice-id",
        required=True,
        help="The custom voice_id you want to create (alphanumeric + underscores, e.g. rocky_linyi_v1)",
    )
    parser.add_argument(
        "--preview-text",
        default=None,
        help="Optional sample text to synthesize during cloning. Disabled by default — cloning with text returned 2013 on our account.",
    )
    parser.add_argument("--model", default=None, help="Optional model override. Leave unset (default) — empirically required.")
    args = parser.parse_args()

    # Load .env from project root (parent of scripts/)
    project_root = Path(__file__).resolve().parent.parent
    load_dotenv(project_root / ".env")

    api_key = _api_key()

    file_id = upload_source_audio(args.audio, api_key)
    body = clone_voice(
        file_id,
        args.voice_id,
        api_key,
        preview_text=args.preview_text,
        model=args.model,
    )

    print()
    print("=" * 60)
    print("✅ Cloning complete!")
    print("=" * 60)
    print(f"   voice_id: {args.voice_id}")
    preview_url = body.get("demo_audio") or body.get("audio_file") or body.get("preview_url")
    if preview_url:
        print(f"   preview:  {preview_url}")
        print("   (open the URL in a browser to hear the cloned voice)")
    print()
    print("Next steps:")
    print(f"  1. Add to .env:    MINIMAX_VOICE_ID={args.voice_id}")
    print("  2. Restart uvicorn (or it'll auto-reload if --reload is on)")
    print("  3. Run Rocky on your iPhone — it now speaks in your cloned voice")
    print()


if __name__ == "__main__":
    main()
