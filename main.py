"""Rocky AI Personal Assistant — FastAPI server with multi-user OAuth.

MiniMax refactor: chat is handled by agents/orchestrator.py (multi-agent).
The OAuth, session, and dashboard layers are unchanged from the Gemini baseline.
"""

import os
from contextlib import asynccontextmanager
from datetime import datetime
from urllib.parse import urlencode

import requests
from dotenv import load_dotenv

# IMPORTANT: load .env BEFORE any project imports — several modules read env
# vars at import time (e.g. llm/t2a.py captures MINIMAX_VOICE_ID into a
# module-level constant), so a late load_dotenv() leaves them stuck on the
# fallback default.
load_dotenv()

from fastapi import Depends, FastAPI, Header, HTTPException
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from agents.orchestrator import process as orchestrate, remember_session_end
from llm import t2a
from memory import bootstrap_contacts, detect_user_email, load_memory, save_memory
from metrics.cost import aggregate as aggregate_metrics
from session import SessionManager
from tracing.tracer import current_trace, end_trace, get_trace, list_traces, start_trace

# Validate MiniMax key on boot — fail fast if misconfigured.
if not os.getenv("MINIMAX_API_KEY"):
    raise RuntimeError("MINIMAX_API_KEY not set. Add it to .env file.")

# OAuth config (multi-user)
GOOGLE_CLIENT_ID = os.getenv("GOOGLE_CLIENT_ID", "")
GOOGLE_CLIENT_SECRET = os.getenv("GOOGLE_CLIENT_SECRET", "")
GOOGLE_REDIRECT_URI = os.getenv("GOOGLE_REDIRECT_URI", "http://localhost:8000/auth/callback")
GOOGLE_SCOPES = "openid email profile https://www.googleapis.com/auth/gmail.modify https://www.googleapis.com/auth/calendar"


# --- Multi-tenant access control ---

def _operator_emails() -> set[str]:
    """Comma-separated allowlist of emails exempt from BYOK enforcement.

    Read at every call (no caching) so .env edits take effect on the next
    request without an uvicorn restart.
    """
    raw = os.getenv("OPERATOR_EMAILS", "")
    return {e.strip().lower() for e in raw.split(",") if e.strip()}


def is_operator(email: str | None) -> bool:
    """True iff the user can use the server's env-var fallback keys.

    Operators don't need to configure their own MiniMax / Brave keys;
    everyone else does (BYOK enforcement at /api/chat).
    """
    if not email:
        return True  # Legacy single-user / token.json mode — fully trusted
    return email.lower() in _operator_emails()


# --- BYOK rejection: pre-synthesized voice prompt ---
#
# When a non-operator user without configured keys hits /api/chat, we want
# Rocky to actually SAY "configure your keys" so the iPhone Shortcut
# experience isn't dead silence. We synthesize this short message once
# (using the operator's env T2A key, since this is a system message —
# not billed to the rejected user) and cache it for all future rejections.
# Zero per-rejection cost / latency after first use.

BYOK_REJECTION_TEXT = (
    "Please configure your MiniMax API key in the settings page before "
    "you can chat with Rocky."
)
BYOK_AUDIO_FILENAME = "byok_rejection.mp3"


def _get_byok_audio_url() -> str | None:
    """Return URL of the pre-synthesized BYOK rejection prompt.

    Lazy: synthesizes on first call, then reuses the on-disk file forever
    (the audio file is in t2a.PERSISTENT_FILES so cleanup_old() skips it).
    Uses operator env keys explicitly — the rejected user has none, but
    even if they did we wouldn't bill them for a system message.
    Returns None if synthesis fails (e.g. no env T2A key configured).
    """
    audio_path = t2a.AUDIO_DIR / BYOK_AUDIO_FILENAME
    if audio_path.exists():
        return f"/audio/{BYOK_AUDIO_FILENAME}"

    # First-call synthesis. Clear any per-user contextvar so resolve_*()
    # falls back to env keys (this audio is a system asset, not user content).
    from tools._user_keys import (
        current_brave_key,
        current_minimax_chat_key,
        current_minimax_payg_key,
    )
    saved_chat = current_minimax_chat_key.get()
    saved_payg = current_minimax_payg_key.get()
    saved_brave = current_brave_key.get()
    current_minimax_chat_key.set(None)
    current_minimax_payg_key.set(None)
    current_brave_key.set(None)
    try:
        result = t2a.synthesize(BYOK_REJECTION_TEXT)
        # synthesize() writes a uuid-named file; rename to our stable name
        # so cleanup skips it and we can reference it by a known URL.
        result.file_path.rename(audio_path)
        print(f"[BYOK] synthesized rejection audio -> {audio_path.name}")
        return f"/audio/{BYOK_AUDIO_FILENAME}"
    except Exception as e:
        print(f"[BYOK] failed to synthesize rejection audio: {e}")
        return None
    finally:
        current_minimax_chat_key.set(saved_chat)
        current_minimax_payg_key.set(saved_payg)
        current_brave_key.set(saved_brave)


# --- Auth middleware ---

async def get_current_user(authorization: str = Header(None)) -> dict:
    """Validate Bearer token and return user dict.

    Falls back to legacy single-user mode if no auth header and token.json exists.
    """
    if authorization and authorization.startswith("Bearer "):
        api_token = authorization.removeprefix("Bearer ").strip()
        try:
            from database import get_user_by_api_token
            user = get_user_by_api_token(api_token)
        except Exception:
            raise HTTPException(status_code=503, detail="Database not configured. Visit /login to set up multi-user mode.")
        if not user:
            raise HTTPException(status_code=401, detail="Invalid API token")
        return user

    # Legacy fallback: if token.json exists, run in single-user mode
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if os.path.exists(token_path):
        return {"id": None, "email": "", "name": ""}

    raise HTTPException(
        status_code=401,
        detail="Missing Authorization header. Visit /login to get your API token.",
    )


# --- Lifespan ---

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Run startup tasks."""
    # Initialize database — only when fully configured AND reachable.
    # The DB only matters for /login multi-user flow; /api/chat works without it
    # in legacy mode (token.json fallback).
    if GOOGLE_CLIENT_ID and not GOOGLE_CLIENT_ID.startswith("your-"):
        try:
            from database import init_db
            init_db()
        except Exception as e:
            print(f"[Startup] DB init skipped — multi-user /login disabled ({e.__class__.__name__})")

    # Legacy single-user bootstrap (when token.json exists)
    token_path = os.path.join(os.path.dirname(__file__), "token.json")
    if os.path.exists(token_path):
        print("[Startup] Legacy mode: detecting user email...")
        try:
            email = detect_user_email()
            print(f"[Startup] User email: {email}")
        except Exception as e:
            print(f"[Startup] Could not detect email: {e}")

        memory = load_memory()
        if not memory.get("contacts"):
            print("[Startup] Bootstrapping contacts from last 3 months...")
            try:
                result = bootstrap_contacts()
                print(f"[Startup] Found {result['contacts_found']} frequent contacts")
            except Exception as e:
                print(f"[Startup] Contact bootstrap failed: {e}")
        else:
            print(f"[Startup] {len(memory['contacts'])} contacts already in memory")
    else:
        print("[Startup] Multi-user mode — no legacy token.json")

    yield  # Server runs


app = FastAPI(title="Rocky AI Personal Assistant", lifespan=lifespan)
session_manager = SessionManager()


# --- Models ---

class ChatRequest(BaseModel):
    message: str
    user_id: str = "default"
    # When true (default), synthesize the reply via MiniMax T2A and return audio_url.
    # iOS Shortcuts can fetch + play the audio for richer voice than Siri's TTS.
    # Set to false for low-latency text-only flows (e.g. dashboard tests).
    tts: bool = True


class ChatResponse(BaseModel):
    reply: str
    action: str = "continue"
    trace_id: str | None = None
    cost_usd: float | None = None
    route: str | None = None
    audio_url: str | None = None  # /audio/{filename}.mp3 when tts=True
    audio_voice: str | None = None  # voice_id used (for trace correlation)


# --- Chat endpoint ---

@app.post("/api/chat", response_model=ChatResponse)
async def chat(request: ChatRequest, user: dict = Depends(get_current_user)):
    """Process a voice command from the iOS Shortcut.

    Multi-agent flow: greeting fast-path → router → specialist. Every turn
    creates a Trace; trace_id and cost are returned for observability.
    """
    user_id = user["id"]  # None for legacy mode

    if not request.message.strip():
        return ChatResponse(reply="I didn't catch that. Could you say it again?")

    # Handle goodbye on the server side for clean shortcut exit.
    # We still synthesize T2A so the user hears Rocky say goodbye before the
    # iOS shortcut loop terminates (otherwise Play Sound has no audio_url and
    # the iPhone plays its error tone instead).
    words = request.message.lower().split()
    if "goodbye" in words or "bye" in words or "bye-bye" in words:
        session_key = user_id or request.user_id
        print(f"\n[User: {session_key}] {request.message}")
        print("[Rocky] Goodbye! (session ending)")
        remember_session_end(user_id)
        session_manager._sessions.pop(session_key, None)

        goodbye_reply = "Goodbye! Talk to you later."
        goodbye_audio_url: str | None = None
        goodbye_voice: str | None = None
        if request.tts:
            try:
                t2a.cleanup_old()
                result = t2a.synthesize(goodbye_reply)
                goodbye_audio_url = f"/audio/{result.filename}"
                goodbye_voice = result.voice_id
            except Exception as e:
                print(f"[T2A] goodbye synthesis failed (text-only): {e}")
        return ChatResponse(
            reply=goodbye_reply,
            action="stop",
            audio_url=goodbye_audio_url,
            audio_voice=goodbye_voice,
        )

    session_key = user_id or request.user_id
    history = session_manager.get_or_create(session_key)
    print(f"\n[User: {session_key}] {request.message}")

    # Load user's stored keys (without setting contextvars yet — the BYOK
    # check below needs to know whether keys exist, but if the user is
    # rejected we want any T2A in the rejection path to use env keys).
    user_keys: dict[str, str | None] = {}
    if user_id:
        try:
            from database import get_user_keys
            user_keys = get_user_keys(user_id)
        except Exception as e:
            # DB lookup is best-effort — chat still works against env keys.
            print(f"[Keys] per-user key load failed (falling back to env): {e}")

    # BYOK enforcement: non-operator users must configure their own MiniMax
    # chat key. Rather than 402 (silent on iPhone), we record a trace AND
    # return a TTS-spoken prompt so the user actually hears why nothing
    # happened. The prompt audio is pre-synthesized + cached forever.
    if not is_operator(user.get("email")) and not user_keys.get("minimax_chat"):
        rej_trace = start_trace(user_message=request.message, user_id=user_id)
        rej_trace.route = "byok_rejected"
        guard_span = rej_trace.add_span("byok.check", "guard")
        guard_span.end(rejected=True, reason="missing minimax_chat key",
                       email=user.get("email"))

        rej_audio_url = _get_byok_audio_url() if request.tts else None
        rej_voice = t2a.DEFAULT_VOICE if rej_audio_url else None

        end_trace(reply=BYOK_REJECTION_TEXT)
        print(f"[BYOK] {user.get('email', '<no email>')} rejected — no minimax_chat key")
        return ChatResponse(
            reply=BYOK_REJECTION_TEXT,
            action="stop",
            audio_url=rej_audio_url,
            audio_voice=rej_voice,
            route="byok_rejected",
            trace_id=rej_trace.trace_id,
            cost_usd=rej_trace.total_cost_usd,
        )

    # Allowed → wire keys to contextvars so all downstream MiniMax / Brave
    # calls bill against the right tenant.
    if user_id:
        from tools._user_keys import (
            current_brave_key,
            current_minimax_chat_key,
            current_minimax_payg_key,
        )
        current_minimax_chat_key.set(user_keys.get("minimax_chat"))
        current_minimax_payg_key.set(user_keys.get("minimax_payg"))
        current_brave_key.set(user_keys.get("brave"))

    trace = start_trace(user_message=request.message, user_id=user_id)

    try:
        reply = orchestrate(request.message, history, user_id=user_id)
    except Exception as e:
        end_trace(reply=f"[error] {e}")
        print(f"[Error] {e}")
        raise HTTPException(
            status_code=500,
            detail="Sorry, something went wrong. Please try again.",
        )

    # Optional voice synthesis. Failure here MUST NOT fail the chat reply —
    # the user still gets text + iPhone Siri TTS as fallback.
    audio_url: str | None = None
    audio_voice: str | None = None
    if request.tts and reply.strip():
        tts_span = trace.add_span("t2a.synthesize", "tool")
        try:
            t2a.cleanup_old()  # opportunistic GC
            result = t2a.synthesize(reply)
            audio_url = f"/audio/{result.filename}"
            audio_voice = result.voice_id
            tts_span.end(
                voice=result.voice_id,
                bytes=result.audio_bytes,
                latency_ms=result.duration_ms,
            )
        except Exception as e:
            tts_span.fail(str(e))
            print(f"[T2A] synthesis failed (text reply still served): {e}")

    end_trace(reply=reply)
    print(f"[Rocky][route={trace.route} cost=${trace.total_cost_usd:.5f}] {reply}")

    return ChatResponse(
        reply=reply,
        trace_id=trace.trace_id,
        cost_usd=round(trace.total_cost_usd, 6),
        route=trace.route,
        audio_url=audio_url,
        audio_voice=audio_voice,
    )


@app.get("/dashboard", response_class=HTMLResponse)
async def dashboard_view():
    """Serve the live observability dashboard.

    Single-file HTML at the project root — polls /metrics + /traces every 2s
    and renders KPI cards, route distribution, and a trace inspector drawer.
    Open this in the browser while sending requests to see the multi-agent
    pipeline working in real time.
    """
    path = os.path.join(os.path.dirname(__file__), "dashboard.html")
    if not os.path.exists(path):
        raise HTTPException(status_code=404, detail="dashboard.html not found")
    with open(path, "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


@app.get("/audio/{filename}")
async def serve_audio(filename: str):
    """Serve a generated audio file. Filename is opaque (uuid hex) — no user
    enumeration risk. Files auto-expire via t2a.cleanup_old().
    """
    # Defend against path traversal — only allow our hex+ext form.
    if "/" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="Invalid filename")
    path = t2a.AUDIO_DIR / filename
    if not path.is_file():
        raise HTTPException(status_code=404, detail="Audio not found (may have expired)")
    media_type = "audio/mpeg" if filename.endswith(".mp3") else "application/octet-stream"
    # NOTE: do NOT pass filename= — it triggers `Content-Disposition: attachment`
    # which makes iOS Shortcuts' Play Sound treat the response as a file download
    # instead of audio data, resulting in silence. Inline streaming is what we want.
    return FileResponse(path, media_type=media_type)


# --- Observability endpoints ---

@app.get("/metrics")
async def metrics(user: dict = Depends(get_current_user)):
    """Aggregate request metrics — total cost, token usage, route breakdown.

    Auth-protected: same Bearer token as /api/chat. Dashboard sends it from
    localStorage; unauthenticated requests get 401.
    """
    return JSONResponse(aggregate_metrics())


@app.get("/trace/{trace_id}")
async def get_trace_endpoint(trace_id: str, user: dict = Depends(get_current_user)):
    """Inspect a single trace — every span (router decision, LLM call, tool call)."""
    trace = get_trace(trace_id)
    if not trace:
        raise HTTPException(status_code=404, detail="Trace not found (may have aged out)")
    return JSONResponse(trace.to_dict())


@app.get("/traces")
async def list_traces_endpoint(limit: int = 50, user: dict = Depends(get_current_user)):
    """Recent traces for the dashboard. Newest first."""
    return JSONResponse({"traces": list_traces(limit=limit)})


# --- Per-user service API keys (multi-tenant) ---

class KeysUpdateRequest(BaseModel):
    # None = leave unchanged. Empty string = explicitly clear (revert to env fallback).
    minimax_chat: str | None = None
    minimax_payg: str | None = None
    brave: str | None = None


@app.get("/api/keys")
async def get_keys(user: dict = Depends(get_current_user)):
    """Return masked status of the user's three service keys.

    Cleartext is NEVER returned — only {"set": bool, "prefix": "sk-cp-AB"}
    so the user can see at a glance which keys are configured.
    """
    if not user.get("id"):
        # Legacy single-user mode has no DB-stored keys.
        return JSONResponse({
            "minimax_chat": {"set": False, "prefix": None, "note": "legacy mode — uses env"},
            "minimax_payg": {"set": False, "prefix": None, "note": "legacy mode — uses env"},
            "brave":        {"set": False, "prefix": None, "note": "legacy mode — uses env"},
        })
    from database import get_user_keys_status
    return JSONResponse(get_user_keys_status(user["id"]))


@app.post("/api/keys")
async def update_keys(req: KeysUpdateRequest, user: dict = Depends(get_current_user)):
    """Update one or more of the user's service keys.

    Send only the fields you want to change. Pass empty string ("") to
    clear a key (reverts that slot to env fallback).
    """
    if not user.get("id"):
        raise HTTPException(
            status_code=400,
            detail="Per-user keys require multi-user mode. Sign in via /login.",
        )
    from database import set_user_keys
    set_user_keys(
        user["id"],
        minimax_chat=req.minimax_chat,
        minimax_payg=req.minimax_payg,
        brave=req.brave,
    )
    # Return the new status so the UI can refresh without a second round-trip.
    from database import get_user_keys_status
    return JSONResponse({
        "ok": True,
        "keys": get_user_keys_status(user["id"]),
    })


# --- Health check ---

@app.get("/health")
async def health():
    """Health check endpoint."""
    return {"status": "ok", "agent": "rocky"}


# --- Debug: inspect / clear sessions (development helper) ---

@app.get("/debug/sessions")
async def debug_sessions():
    """Dump every active in-memory session — for diagnosing history pollution."""
    return JSONResponse({
        "count": len(session_manager._sessions),
        "sessions": {
            uid: {
                "history_len": len(s["history"]),
                "history": s["history"],
                "last_access": s["last_access"],
            }
            for uid, s in session_manager._sessions.items()
        },
    })


@app.post("/debug/clear-sessions")
async def debug_clear_sessions():
    """Wipe every in-memory session. Use when history gets corrupted."""
    n = len(session_manager._sessions)
    session_manager._sessions.clear()
    return {"cleared": n}


# --- OAuth routes ---

@app.get("/", response_class=HTMLResponse)
async def root():
    """Smart landing — returning users (token in localStorage) go straight to
    /dashboard; new visitors land on /login. Rendered as a tiny HTML page
    rather than a server-side redirect because we need to check localStorage,
    which only the browser can see."""
    return HTMLResponse(_ROOT_HTML)


@app.get("/login", response_class=HTMLResponse)
async def login_page():
    """Serve the login page with Sign in with Google button."""
    if not GOOGLE_CLIENT_ID:
        return HTMLResponse(
            "<h2>OAuth not configured</h2><p>Set GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET in .env</p>",
            status_code=503,
        )
    return HTMLResponse(_LOGIN_HTML)


@app.get("/auth/google")
async def auth_google():
    """Redirect to Google's OAuth consent screen."""
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": GOOGLE_REDIRECT_URI,
        "response_type": "code",
        "scope": GOOGLE_SCOPES,
        "access_type": "offline",
        "prompt": "consent",
    }
    url = f"https://accounts.google.com/o/oauth2/v2/auth?{urlencode(params)}"
    return RedirectResponse(url)


@app.get("/auth/callback")
async def auth_callback(code: str):
    """Handle Google OAuth callback — exchange code for tokens and create user."""
    from database import create_user, get_user_by_google_id, update_user_tokens

    # Exchange authorization code for tokens
    token_response = requests.post(
        "https://oauth2.googleapis.com/token",
        data={
            "code": code,
            "client_id": GOOGLE_CLIENT_ID,
            "client_secret": GOOGLE_CLIENT_SECRET,
            "redirect_uri": GOOGLE_REDIRECT_URI,
            "grant_type": "authorization_code",
        },
    )

    if token_response.status_code != 200:
        raise HTTPException(status_code=400, detail="Failed to exchange authorization code")

    tokens = token_response.json()
    access_token = tokens["access_token"]
    refresh_token = tokens.get("refresh_token", "")

    if not refresh_token:
        raise HTTPException(
            status_code=400,
            detail="No refresh token received. Please revoke access at https://myaccount.google.com/permissions and try again.",
        )

    # Get user info from Google
    userinfo_response = requests.get(
        "https://www.googleapis.com/oauth2/v2/userinfo",
        headers={"Authorization": f"Bearer {access_token}"},
    )
    userinfo = userinfo_response.json()
    if userinfo_response.status_code != 200 or "id" not in userinfo:
        print(f"[OAuth] Userinfo error: {userinfo}")
        raise HTTPException(status_code=400, detail=f"Failed to get user info from Google: {userinfo.get('error', 'unknown error')}")
    google_id = userinfo["id"]
    email = userinfo.get("email", "")
    name = userinfo.get("name", "")

    # Create or update user
    existing = get_user_by_google_id(google_id)
    if existing:
        update_user_tokens(
            google_id,
            access_token,
            tokens.get("expires_in", ""),
            refresh_token,
        )
        api_token = existing["api_token"]
    else:
        user = create_user(google_id, email, name, refresh_token)
        api_token = user["api_token"]

        # Bootstrap contacts AND RAG email index for new user.
        # Synchronous on first login is fine for demo; for production, fire
        # this off as a background task (asyncio.create_task or Celery worker).
        try:
            from auth import get_credentials_for_user
            creds = get_credentials_for_user(google_id)
            detect_user_email(user_id=google_id, credentials=creds)
            bootstrap_contacts(user_id=google_id, credentials=creds)
            print(f"[OAuth] Bootstrapped contacts for {email}")
        except Exception as e:
            print(f"[OAuth] Contact bootstrap failed for {email}: {e}")

        try:
            from rag.email_indexer import backfill
            result = backfill(user_id=google_id, credentials=creds)
            print(f"[OAuth] RAG indexed {result['indexed']} emails for {email}")
        except Exception as e:
            print(f"[OAuth] RAG backfill failed for {email}: {e}")

    # Decide whether to flag the BYOK prompt on /setup. Non-operator users
    # without a MiniMax chat key in DB will hit 402 on /api/chat, so we
    # surface a clear banner on /setup with a deeplink to /settings instead
    # of letting them discover it the hard way.
    needs_keys = "0"
    if not is_operator(email):
        try:
            from database import get_user_keys
            user_keys = get_user_keys(google_id)
            if not user_keys.get("minimax_chat"):
                needs_keys = "1"
        except Exception as e:
            print(f"[OAuth] Could not check keys for {email}: {e}")

    return RedirectResponse(
        f"/setup?token={api_token}&name={name}&needs_keys={needs_keys}"
    )


@app.get("/setup", response_class=HTMLResponse)
async def setup_page(token: str, name: str = "", needs_keys: str = "0"):
    """Show post-login page with API token and iOS Shortcut setup instructions.

    Renamed from /dashboard so the live observability dashboard can own that
    canonical URL.

    needs_keys=1 surfaces a prominent BYOK banner — set by /auth/callback
    when the just-logged-in user is non-operator and has no minimax_chat
    key configured yet. They'd otherwise hit 402 on first /api/chat.
    """
    # Derive server URL from GOOGLE_REDIRECT_URI (strip /auth/callback)
    server_url = GOOGLE_REDIRECT_URI.replace("/auth/callback", "")
    return HTMLResponse(_dashboard_html(token, name, server_url, needs_keys=needs_keys == "1"))


@app.get("/settings", response_class=HTMLResponse)
async def settings_page():
    """Per-user service-key settings page.

    Static HTML — auth happens client-side via Bearer token in localStorage
    (same pattern as /dashboard). The page calls GET /api/keys on load and
    POST /api/keys on save.
    """
    return HTMLResponse(_SETTINGS_HTML)


# --- HTML Templates ---

_ROOT_HTML = """<!DOCTYPE html>
<html><head><meta charset="UTF-8"><title>Rocky</title>
<style>body{background:#0d0d1a;color:#fff;font-family:-apple-system,'Segoe UI',sans-serif;
display:flex;align-items:center;justify-content:center;min-height:100vh;margin:0;
font-size:14px;color:rgba(255,255,255,0.6);}</style></head>
<body>Routing…
<script>
// Has the user authenticated before? Send them straight to dashboard.
// Otherwise kick them to /login. Done client-side so localStorage is visible.
const t = localStorage.getItem('rocky_api_token');
location.replace(t ? '/dashboard' : '/login');
</script>
</body></html>"""


_LOGIN_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rocky — Sign In</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    min-height: 100vh;
    background: linear-gradient(135deg, #0d0d1a 0%, #1a1a2e 40%, #16213e 70%, #1a1035 100%);
    display: flex; align-items: center; justify-content: center;
    font-family: -apple-system, 'Segoe UI', sans-serif; color: white;
  }
  .card {
    background: rgba(255,255,255,0.06);
    border: 1px solid rgba(255,255,255,0.1);
    border-radius: 24px; padding: 48px;
    text-align: center; max-width: 420px; width: 90%;
    backdrop-filter: blur(20px);
  }
  .logo { font-size: 56px; font-weight: 700; margin-bottom: 8px;
    background: linear-gradient(135deg, #FF9A6C, #FF6B8A);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }
  .subtitle { color: rgba(255,255,255,0.5); font-size: 18px; margin-bottom: 32px; }
  .desc { color: rgba(255,255,255,0.4); font-size: 14px; line-height: 1.6; margin-bottom: 32px; }
  .google-btn {
    display: inline-flex; align-items: center; gap: 12px;
    background: white; color: #333; border: none; border-radius: 12px;
    padding: 14px 32px; font-size: 16px; font-weight: 500;
    cursor: pointer; text-decoration: none;
    transition: transform 0.15s, box-shadow 0.15s;
  }
  .google-btn:hover { transform: translateY(-2px); box-shadow: 0 8px 24px rgba(0,0,0,0.3); }
  .google-btn svg { width: 20px; height: 20px; }
</style>
</head>
<body>
<div class="card">
  <div class="logo">Rocky</div>
  <div class="subtitle">Voice AI Personal Assistant</div>
  <div class="desc">
    Sign in with your Google account to let Rocky manage your Gmail,
    Calendar, and more — all through natural voice conversation.
  </div>
  <a class="google-btn" href="/auth/google">
    <svg viewBox="0 0 24 24"><path fill="#4285F4" d="M22.56 12.25c0-.78-.07-1.53-.2-2.25H12v4.26h5.92a5.06 5.06 0 0 1-2.2 3.32v2.77h3.57c2.08-1.92 3.28-4.74 3.28-8.1z"/><path fill="#34A853" d="M12 23c2.97 0 5.46-.98 7.28-2.66l-3.57-2.77c-.98.66-2.23 1.06-3.71 1.06-2.86 0-5.29-1.93-6.16-4.53H2.18v2.84C3.99 20.53 7.7 23 12 23z"/><path fill="#FBBC05" d="M5.84 14.09c-.22-.66-.35-1.36-.35-2.09s.13-1.43.35-2.09V7.07H2.18C1.43 8.55 1 10.22 1 12s.43 3.45 1.18 4.93l2.85-2.22.81-.62z"/><path fill="#EA4335" d="M12 5.38c1.62 0 3.06.56 4.21 1.64l3.15-3.15C17.45 2.09 14.97 1 12 1 7.7 1 3.99 3.47 2.18 7.07l3.66 2.84c.87-2.6 3.3-4.53 6.16-4.53z"/></svg>
    Sign in with Google
  </a>
</div>
</body>
</html>"""


_SETTINGS_HTML = """<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rocky — Settings</title>
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    min-height: 100vh; font-family: -apple-system, 'Segoe UI', sans-serif;
    background: linear-gradient(135deg, #0d0d1a 0%, #1a1a2e 40%, #16213e 70%, #1a1035 100%);
    color: #e7e8ee; padding: 40px 20px;
  }
  .wrap { max-width: 720px; margin: 0 auto; }
  .header { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 32px; }
  h1 {
    font-size: 32px; font-weight: 700; letter-spacing: -0.5px;
    background: linear-gradient(90deg, #ff9a6c, #ff6b8a);
    -webkit-background-clip: text; background-clip: text; -webkit-text-fill-color: transparent;
  }
  .subtitle { color: #8b8d9b; margin-top: 6px; font-size: 14px; }
  .back { color: #8b8d9b; font-size: 13px; text-decoration: none; }
  .back:hover { color: #e7e8ee; }
  .panel {
    background: rgba(20,22,38,0.6); border: 1px solid rgba(255,255,255,0.06);
    border-radius: 14px; padding: 24px; margin-top: 16px;
  }
  .field { margin-bottom: 22px; }
  .field:last-child { margin-bottom: 0; }
  .label-row { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 8px; }
  label { font-size: 14px; font-weight: 600; }
  .status { font-family: ui-monospace, 'SF Mono', monospace; font-size: 11px;
    padding: 3px 8px; border-radius: 4px; }
  .status.set { background: rgba(52,211,153,0.18); color: #34d399; }
  .status.unset { background: rgba(231,232,238,0.08); color: #8b8d9b; }
  .desc { color: #8b8d9b; font-size: 12px; margin-bottom: 8px; line-height: 1.5; }
  input[type="password"], input[type="text"] {
    width: 100%; background: rgba(255,255,255,0.04); border: 1px solid rgba(255,255,255,0.08);
    color: #e7e8ee; padding: 12px 14px; border-radius: 8px; font-size: 14px;
    font-family: ui-monospace, 'SF Mono', monospace;
  }
  input:focus { outline: none; border-color: #ff6b8a; }
  .actions { margin-top: 32px; display: flex; gap: 12px; align-items: center; }
  button {
    background: linear-gradient(90deg, #ff9a6c, #ff6b8a); color: white;
    border: none; padding: 12px 24px; border-radius: 8px; font-weight: 600;
    cursor: pointer; font-size: 14px;
  }
  button:hover { filter: brightness(1.1); }
  button.ghost { background: transparent; border: 1px solid rgba(255,255,255,0.12); color: #e7e8ee; }
  .msg { padding: 10px 14px; border-radius: 8px; font-size: 13px; margin-top: 16px; display: none; }
  .msg.ok { background: rgba(52,211,153,0.15); color: #34d399; display: block; }
  .msg.err { background: rgba(255,107,138,0.15); color: #ff6b8a; display: block; }
  .login-overlay {
    position: fixed; inset: 0; background: rgba(13,13,26,0.95);
    display: flex; align-items: center; justify-content: center;
  }
  .login-card { background: rgba(20,22,38,0.9); padding: 32px; border-radius: 14px;
    width: 360px; max-width: 90vw; border: 1px solid rgba(255,255,255,0.08); }
  .hint { color: #8b8d9b; font-size: 12px; margin-top: 8px; line-height: 1.5; }
</style>
</head>
<body>

<div id="login" class="login-overlay">
  <div class="login-card">
    <h1 style="font-size:22px; margin-bottom:16px;">Rocky Settings</h1>
    <p class="hint" style="margin-bottom:14px;">Paste your API token to manage your service keys.</p>
    <input type="password" id="token-input" placeholder="API token" />
    <div class="actions"><button onclick="signIn()">Continue</button></div>
    <div id="login-msg" class="msg"></div>
  </div>
</div>

<div id="app" class="wrap" style="display:none;">
  <div class="header">
    <div>
      <h1>Service API Keys</h1>
      <div class="subtitle">Configure your own MiniMax + Brave keys so usage bills to your account, not the operator's.</div>
    </div>
    <a class="back" href="/dashboard">← Dashboard</a>
  </div>

  <div class="panel">
    <div class="field">
      <div class="label-row">
        <label for="minimax_chat">MiniMax chat key</label>
        <span id="status-minimax_chat" class="status unset">unset</span>
      </div>
      <div class="desc">Token Plan key (sk-cp-...) for M2.7 chat. Get one at platform.minimax.io.</div>
      <input type="password" id="minimax_chat" placeholder="sk-cp-..." autocomplete="off" />
    </div>

    <div class="field">
      <div class="label-row">
        <label for="minimax_payg">MiniMax pay-as-you-go key</label>
        <span id="status-minimax_payg" class="status unset">unset</span>
      </div>
      <div class="desc">Pay-as-you-go key (sk-api-...) for T2A voice + embeddings. Token Plan doesn't cover these.</div>
      <input type="password" id="minimax_payg" placeholder="sk-api-..." autocomplete="off" />
    </div>

    <div class="field">
      <div class="label-row">
        <label for="brave">Brave Search key</label>
        <span id="status-brave" class="status unset">unset</span>
      </div>
      <div class="desc">Brave Search API key. Free tier 2K queries/month at brave.com/search/api/.</div>
      <input type="password" id="brave" placeholder="BSA..." autocomplete="off" />
    </div>

    <div class="actions">
      <button onclick="save()">Save</button>
      <button class="ghost" onclick="signOut()">Sign out</button>
    </div>
    <div id="msg" class="msg"></div>
  </div>

  <div class="panel" style="margin-top:24px;">
    <div class="subtitle" style="margin:0;">
      <strong style="color:#e7e8ee;">How resolution works:</strong>
      For each request, Rocky checks your stored key first, then falls back to
      the server's env-var defaults. Leave a field blank to keep the existing
      value; type "clear" to remove a stored key. Stored keys are encrypted
      with Fernet using the server's TOKEN_ENCRYPTION_KEY.
    </div>
  </div>
</div>

<script>
const $ = (id) => document.getElementById(id);
const TOKEN_KEY = 'rocky_api_token';

function getToken() { return localStorage.getItem(TOKEN_KEY); }
function setToken(t) { localStorage.setItem(TOKEN_KEY, t); }

async function authedFetch(url, opts = {}) {
  const t = getToken();
  if (!t) { showLogin(); throw new Error('no token'); }
  opts.headers = Object.assign({}, opts.headers, { 'Authorization': `Bearer ${t}` });
  const r = await fetch(url, opts);
  if (r.status === 401) { localStorage.removeItem(TOKEN_KEY); showLogin(); throw new Error('unauthorized'); }
  return r;
}

function showLogin() { $('login').style.display = 'flex'; $('app').style.display = 'none'; }
function showApp()   { $('login').style.display = 'none'; $('app').style.display = 'block'; }

async function signIn() {
  const t = $('token-input').value.trim();
  if (!t) return;
  $('login-msg').className = 'msg'; $('login-msg').textContent = 'Validating...';
  try {
    const r = await fetch('/api/keys', { headers: { 'Authorization': `Bearer ${t}` } });
    if (r.ok) { setToken(t); showApp(); refresh(); }
    else { $('login-msg').className = 'msg err'; $('login-msg').textContent = 'Invalid token.'; }
  } catch (e) { $('login-msg').className = 'msg err'; $('login-msg').textContent = String(e); }
}

function signOut() { localStorage.removeItem(TOKEN_KEY); showLogin(); }

async function refresh() {
  const r = await authedFetch('/api/keys');
  const data = await r.json();
  for (const name of ['minimax_chat', 'minimax_payg', 'brave']) {
    const s = data[name] || {};
    const el = $('status-' + name);
    if (s.set) {
      el.textContent = (s.prefix || '...') + ' (set)';
      el.className = 'status set';
    } else {
      el.textContent = 'using env fallback';
      el.className = 'status unset';
    }
  }
}

async function save() {
  const body = {};
  for (const name of ['minimax_chat', 'minimax_payg', 'brave']) {
    const v = $(name).value.trim();
    if (!v) continue;                      // blank → leave unchanged
    if (v.toLowerCase() === 'clear') body[name] = '';  // explicit clear
    else body[name] = v;
  }
  if (Object.keys(body).length === 0) {
    $('msg').className = 'msg err'; $('msg').textContent = 'Nothing to save.'; return;
  }

  $('msg').className = 'msg'; $('msg').textContent = 'Saving...';
  try {
    const r = await authedFetch('/api/keys', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    if (!r.ok) throw new Error(`HTTP ${r.status}`);
    $('msg').className = 'msg ok'; $('msg').textContent = 'Saved.';
    for (const name of ['minimax_chat', 'minimax_payg', 'brave']) $(name).value = '';
    refresh();
  } catch (e) {
    $('msg').className = 'msg err'; $('msg').textContent = 'Save failed: ' + e;
  }
}

if (getToken()) { showApp(); refresh(); } else { showLogin(); }
$('token-input').addEventListener('keydown', (e) => { if (e.key === 'Enter') signIn(); });
</script>
</body>
</html>"""


def _dashboard_html(token: str, name: str, server_url: str = "", needs_keys: bool = False) -> str:
    display_name = name or "there"
    api_url = f"{server_url}/api/chat" if server_url else "/api/chat"

    # BYOK prompt banner — shown only when the just-logged-in user is
    # non-operator and has no minimax_chat key in DB. Without this prompt
    # they'd discover the requirement only when /api/chat returns 402.
    needs_keys_banner = ""
    if needs_keys:
        needs_keys_banner = """
  <div class="byok-alert">
    <div class="byok-alert-icon">⚠</div>
    <div class="byok-alert-body">
      <div class="byok-alert-title">Now configure your API keys</div>
      <div class="byok-alert-desc">
        With the token above, set your own MiniMax + Brave keys at the
        link below. Rocky uses your keys so usage bills to your account,
        not the operator's. Takes one minute.
      </div>
      <a class="byok-alert-cta" href="/settings">Configure API keys →</a>
    </div>
  </div>
"""
    return f"""<!DOCTYPE html>
<html>
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Rocky — Setup Guide</title>
<style>
  * {{ margin: 0; padding: 0; box-sizing: border-box; }}
  body {{
    min-height: 100vh;
    background: linear-gradient(135deg, #0d0d1a 0%, #1a1a2e 40%, #16213e 70%, #1a1035 100%);
    font-family: -apple-system, 'Segoe UI', sans-serif; color: white;
    padding: 40px 16px;
  }}
  .container {{ max-width: 540px; margin: 0 auto; }}

  /* Header */
  .header {{ text-align: center; margin-bottom: 36px; }}
  .logo {{ font-size: 42px; font-weight: 700;
    background: linear-gradient(135deg, #FF9A6C, #FF6B8A);
    -webkit-background-clip: text; -webkit-text-fill-color: transparent; }}
  .welcome {{ font-size: 18px; margin-top: 8px; color: rgba(255,255,255,0.6); }}

  /* Steps */
  .step {{ display: flex; gap: 16px; margin-bottom: 28px; }}
  .step-num {{
    flex-shrink: 0; width: 32px; height: 32px; border-radius: 50%;
    background: linear-gradient(135deg, #FF9A6C, #FF6B8A);
    display: flex; align-items: center; justify-content: center;
    font-weight: 700; font-size: 15px; color: #0d0d1a; margin-top: 2px;
  }}
  .step-num.done {{ background: #34A853; font-size: 16px; }}
  .step-body {{ flex: 1; }}
  .step-title {{ font-size: 17px; font-weight: 600; margin-bottom: 6px; color: rgba(255,255,255,0.9); }}
  .step-desc {{ font-size: 14px; color: rgba(255,255,255,0.45); line-height: 1.6; }}
  .step-desc strong {{ color: rgba(255,255,255,0.7); }}

  /* Copyable value */
  .copy-box {{
    background: rgba(0,0,0,0.35); border: 1px solid rgba(255,255,255,0.12);
    border-radius: 10px; padding: 12px 14px; font-family: 'SF Mono', monospace;
    font-size: 13px; word-break: break-all; color: #FF9A6C;
    cursor: pointer; position: relative; margin: 8px 0;
    transition: background 0.15s;
  }}
  .copy-box:hover {{ background: rgba(0,0,0,0.55); }}
  .copy-box .hint {{ position: absolute; right: 10px; top: 50%; transform: translateY(-50%);
    font-family: sans-serif; font-size: 11px; color: rgba(255,255,255,0.25);
    pointer-events: none; }}
  .copy-box .hint.ok {{ color: #34A853; }}

  /* Token box (larger) */
  .token-box {{ font-size: 15px; padding: 14px 16px; letter-spacing: 0.3px; }}

  /* Config rows */
  .config {{ margin: 10px 0 4px; }}
  .config-label {{ font-size: 11px; color: rgba(255,255,255,0.3); text-transform: uppercase;
    letter-spacing: 0.8px; margin-bottom: 4px; }}

  /* Download buttons */
  .dl-btn {{
    display: flex; align-items: center; gap: 10px;
    background: rgba(255,255,255,0.08); border: 1px solid rgba(255,255,255,0.15);
    border-radius: 12px; padding: 14px 18px; text-decoration: none;
    color: white; font-size: 15px; font-weight: 500;
    margin: 8px 0; transition: all 0.15s;
  }}
  .dl-btn:hover {{ background: rgba(255,255,255,0.14); transform: translateY(-1px); }}
  .dl-btn .icon {{ font-size: 22px; }}
  .dl-btn .meta {{ flex: 1; }}
  .dl-btn .dl-name {{ font-weight: 600; }}
  .dl-btn .dl-desc {{ font-size: 12px; color: rgba(255,255,255,0.4); margin-top: 2px; }}
  .dl-btn .arrow {{ color: rgba(255,255,255,0.3); font-size: 18px; }}

  /* Sub-steps */
  .sub-steps {{ margin: 10px 0 0; padding-left: 0; list-style: none; }}
  .sub-steps li {{
    font-size: 13px; color: rgba(255,255,255,0.45); line-height: 1.7;
    padding: 3px 0 3px 20px; position: relative;
  }}
  .sub-steps li::before {{
    content: ''; position: absolute; left: 4px; top: 10px;
    width: 6px; height: 6px; border-radius: 50%;
    background: rgba(255,154,108,0.4);
  }}
  .sub-steps code {{
    background: rgba(255,255,255,0.08); padding: 1px 6px; border-radius: 4px;
    font-size: 12px; color: #FF9A6C;
  }}

  /* Divider */
  .divider {{ border: none; border-top: 1px solid rgba(255,255,255,0.06); margin: 32px 0; }}

  /* Curl section */
  .curl-toggle {{
    font-size: 13px; color: rgba(255,255,255,0.3); cursor: pointer;
    text-align: center; padding: 8px;
  }}
  .curl-toggle:hover {{ color: rgba(255,255,255,0.5); }}
  .curl-content {{ display: none; margin-top: 8px; }}
  .curl-content.show {{ display: block; }}

  /* BYOK first-time prompt banner */
  .byok-alert {{
    display: flex; gap: 14px; align-items: flex-start;
    background: linear-gradient(135deg, rgba(255,154,108,0.15), rgba(255,107,138,0.15));
    border: 1px solid rgba(255,154,108,0.4);
    border-radius: 12px; padding: 18px 18px 18px 16px; margin-bottom: 28px;
  }}
  .byok-alert-icon {{
    flex-shrink: 0; font-size: 20px; line-height: 1.2;
    color: #FF9A6C;
  }}
  .byok-alert-body {{ flex: 1; }}
  .byok-alert-title {{ font-size: 15px; font-weight: 700; color: white;
    margin-bottom: 4px; }}
  .byok-alert-desc {{ font-size: 13px; color: rgba(255,255,255,0.70);
    line-height: 1.5; margin-bottom: 12px; }}
  .byok-alert-cta {{
    display: inline-block; padding: 9px 16px;
    background: linear-gradient(135deg, #FF9A6C, #FF6B8A);
    color: #0d0d1a; text-decoration: none; font-weight: 700; font-size: 13px;
    border-radius: 8px;
  }}
  .byok-alert-cta:hover {{ filter: brightness(1.08); }}
</style>
</head>
<body>
<div class="container">

  <!-- Header -->
  <div class="header">
    <div class="logo">Rocky</div>
    <div class="welcome">Welcome, {display_name}! Follow the steps below to get started.</div>
  </div>

  <!-- Step 1: API Token -->
  <div class="step">
    <div class="step-num">1</div>
    <div class="step-body">
      <div class="step-title">Copy Your API Token</div>
      <div class="step-desc">This is your personal token. Tap to copy it — you'll paste it into the shortcuts below.</div>
      <div class="copy-box token-box" onclick="copyText(this, '{token}')">
        {token}
        <span class="hint">tap to copy</span>
      </div>
    </div>
  </div>
{needs_keys_banner}
  <!-- Step 2: Download Shortcuts -->
  <div class="step">
    <div class="step-num">2</div>
    <div class="step-body">
      <div class="step-title">Install the Shortcuts</div>
      <div class="step-desc">Download both shortcuts to your iPhone. Open each link in Safari and tap <strong>Add Shortcut</strong>.</div>
      <a class="dl-btn" href="https://www.icloud.com/shortcuts/d57fa0a81e7945498f10f074d1cbf3a3" target="_blank">
        <span class="icon">&#9749;</span>
        <span class="meta">
          <span class="dl-name">Rocky</span>
          <span class="dl-desc">Main voice assistant — starts the conversation</span>
        </span>
        <span class="arrow">&#8250;</span>
      </a>
      <a class="dl-btn" href="https://www.icloud.com/shortcuts/71534523de9747c281c8cc6eb8276a5f" target="_blank">
        <span class="icon">&#128172;</span>
        <span class="meta">
          <span class="dl-name">Rocky Chat</span>
          <span class="dl-desc">Conversation handler — keeps the dialogue going</span>
        </span>
        <span class="arrow">&#8250;</span>
      </a>
    </div>
  </div>

  <!-- Step 3: Configure -->
  <div class="step">
    <div class="step-num">3</div>
    <div class="step-body">
      <div class="step-title">Configure the Shortcuts</div>
      <div class="step-desc">Open <strong>each</strong> shortcut in the Shortcuts app, tap the <strong>&#8943;</strong> menu to edit, and find the <code>Text</code> actions at the top. Replace the placeholder values:</div>
      <div class="config">
        <div class="config-label">Server URL</div>
        <div class="copy-box" onclick="copyText(this, '{api_url}')">{api_url}<span class="hint">tap to copy</span></div>
      </div>
      <div class="config">
        <div class="config-label">API Token</div>
        <div class="copy-box" onclick="copyText(this, '{token}')">{token}<span class="hint">tap to copy</span></div>
      </div>
      <ul class="sub-steps">
        <li>Open each shortcut and tap the <strong>&#8943;</strong> (three dots) to edit</li>
        <li>Find the <code>Text</code> field containing the server URL — paste yours</li>
        <li>Find the <code>Text</code> field containing the API token — paste yours</li>
        <li>Tap <strong>Done</strong> to save</li>
      </ul>
    </div>
  </div>

  <!-- Step 4: Vocal Shortcut -->
  <div class="step">
    <div class="step-num">4</div>
    <div class="step-body">
      <div class="step-title">Set Up "Hi Rocky" Voice Trigger</div>
      <div class="step-desc">This lets you start Rocky hands-free — just say <strong>"Hi Rocky"</strong> anytime.</div>
      <ul class="sub-steps">
        <li>Open <strong>Settings</strong> on your iPhone</li>
        <li>Go to <strong>Accessibility</strong> &#8250; <strong>Vocal Shortcuts</strong></li>
        <li>Enable <strong>Vocal Shortcuts</strong> toggle</li>
        <li>Tap <strong>Add Action</strong></li>
        <li>Choose <strong>Run Shortcut</strong>, then select <strong>Rocky</strong></li>
        <li>Set the custom phrase to <strong>"Hi Rocky"</strong></li>
        <li>Tap <strong>Save</strong></li>
      </ul>
    </div>
  </div>

  <!-- Step 5: Done -->
  <div class="step">
    <div class="step-num done">&#10003;</div>
    <div class="step-body">
      <div class="step-title">You're All Set!</div>
      <div class="step-desc">
        Say <strong>"Hi Rocky"</strong> to start a conversation. Rocky can read your emails,
        manage your calendar, search the web, and remember things for you.
        Say <strong>"Goodbye"</strong> to end a session.
      </div>
    </div>
  </div>

  <hr class="divider">

  <!-- Per-user API keys -->
  <div style="margin: 24px 0;">
    <div style="font-size: 14px; color: rgba(255,255,255,0.85); margin-bottom: 8px;">
      Want to bill MiniMax / Brave usage to your own account?
    </div>
    <a href="/settings" style="display: inline-block; padding: 10px 18px; background: rgba(255,154,108,0.18); color: #ff9a6c; text-decoration: none; border-radius: 8px; font-size: 13px; font-weight: 600;">
      Configure your API keys →
    </a>
  </div>

  <hr class="divider">

  <!-- Developer API -->
  <div class="curl-toggle" onclick="this.nextElementSibling.classList.toggle('show')">
    Developer? Show API usage &#9662;
  </div>
  <div class="curl-content">
    <div class="copy-box" style="font-size: 12px; color: rgba(255,255,255,0.5); white-space: pre; overflow-x: auto;" onclick="copyText(this, 'curl -X POST {api_url} -H &quot;Authorization: Bearer {token}&quot; -H &quot;Content-Type: application/json&quot; -d \\'{{\\'message\\': \\'read my emails\\'}}\\'' )">curl -X POST {api_url} \
  -H "Authorization: Bearer {token}" \
  -H "Content-Type: application/json" \
  -d '{{"message": "read my emails"}}'<span class="hint">tap to copy</span></div>
  </div>
</div>

<script>
function copyText(el, text) {{
  navigator.clipboard.writeText(text).then(function() {{
    var hint = el.querySelector('.hint');
    hint.textContent = 'Copied!';
    hint.classList.add('ok');
    setTimeout(function() {{
      hint.textContent = 'tap to copy';
      hint.classList.remove('ok');
    }}, 2000);
  }});
}}
</script>
</body>
</html>"""
