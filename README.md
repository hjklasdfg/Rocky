# Rocky — Multi-Agent Voice Assistant on MiniMax

🌐 **English** · [中文](README_zh.md)

> **"Hi Rocky, what's on my schedule tomorrow?"** — "10am engineering review with Wei, 2pm contract call with Sarah." (spoken in my own cloned voice)
> **"What did Sarah say last month about the contract?"** — "Sarah's March 12 email said she wants to push the Q2 renewal price up 8%." (millisecond RAG hit)
> **"Add an engineering review Friday at 4pm with Wei."** — "Done, scheduled for Friday at 4pm."
> **"Reply to Sarah saying I'll be there at 3."** — "Sent." (threaded under the original email)

Rocky compared to other products:

- **Siri** — iOS voice + Apple Intelligence, but the on-device LLM is small, the agent model is closed, and users can't register their own specialists or SaaS data sources.
- **ChatGPT** — strong multimodal interaction, but no access to Gmail / Calendar / personal SaaS data.
- **OpenClaw** — cloud LLM + local-data Skills, but no iOS voice yet.
- **Rocky** — native iOS voice, **MiniMax-M2.7** driving 5 tool-calling specialists, read/write access to the user's Gmail / Calendar / long-term memory, plus web search and RAG. Replies in the user's own voice via MiniMax voice cloning.

**Multi-tenant by design**: each user configures their own MiniMax + Brave Search API keys (BYOK), all credentials Fernet-encrypted in PostgreSQL and per-request scoped via Python ContextVars. Sign in with Google OAuth at `/login`, configure keys at `/settings`, then use Rocky from the iOS Shortcut or the live dashboard.

---

## Highlights

| | Rocky |
|---|---|
| LLM | **MiniMax-M2.7** (OpenAI-compatible API) |
| Web search | **Brave Search API** (independent index, AI-Grounding endpoint) |
| Architecture | **Router → 5 specialist agents** (email, calendar, web, memory, knowledge) |
| Tool registration | **Versioned OpenAI tool schemas** (`tools/schemas.py`) |
| Prompts | **6 versioned prompts** (`prompts/v1/*.md`, `PROMPT_VERSION` env to swap) |
| RAG | **Local vector store** (sentence-transformers + numpy) over 6 months of email via Gmail OAuth. Store layer is source-agnostic — adding more OAuth-able sources (Notion, GDrive, Slack) is a ~30-line indexer + OAuth wiring; truly-local files would require an upload endpoint or a per-user sync agent |
| Voice | **MiniMax speech-2.8-hd** T2A + voice cloning (returns `audio_url`); BYOK rejection prompt is pre-synthesised once and reused |
| Multi-tenant | **Per-user MiniMax / Brave keys** (BYOK), Fernet-encrypted in PG, ContextVar-injected per request; operator allowlist lets trusted users skip BYOK and share the server's quota |
| Observability | **Per-request traces** (incl. BYOK rejections), `/metrics`, live `/dashboard` |
| Eval | **20-case eval suite** with routing / tool-call / latency / cost metrics |

---

## Architecture

```
                   iPhone — "Hi Rocky" voice trigger
                                    ↓
                        POST /api/chat  {message, tts: true}
                                    ↓
                          Auth + Session (PG, multi-user)
                                    ↓
                       ┌────────── Router ──────────┐
                       │  agents/router.py          │
                       │  Heuristics first (0¢/0ms) │
                       │  LLM fallback (M2.7, JSON) │
                       └────────────┬───────────────┘
                                    │
   ┌────────────┬────────────┬──────┴──────┬─────────────┬───────────────┐
   ↓            ↓            ↓             ↓             ↓               ↓
 Greeting    Email        Calendar       Web           Memory        Knowledge
 FastPath    Agent        Agent          Agent         Agent         (RAG)
 (no LLM)    │            │              │             │              │
             Gmail API    Calendar API   Brave Search  user memory    numpy store
             5 tools      5 tools        AI Grounding  save/delete    vector search
                                    │
                                    ↓ final reply text
                       ┌─── MiniMax speech-2.8-hd T2A ───┐
                       │  llm/t2a.py                   │
                       │  Returns audio_url            │
                       └───────────────────────────────┘
                                    ↓
              { reply, audio_url, route, cost_usd, trace_id }
                                    ↓
              iOS Shortcut plays audio_url + repeats loop
```

**Data flow surfaces:**
- `POST /api/chat` — voice command in, structured reply + audio out
- `GET /audio/{id}` — serves generated mp3 (1h TTL, auto-cleanup)
- `GET /dashboard` — live observability UI (KPIs, route distribution, trace drilldown)
- `GET /metrics` — JSON aggregate for Prometheus-style scrapers
- `GET /trace/{id}` — single-request span tree
- `GET /login` → `/setup` — Google OAuth multi-user signup
- `python -m evals.run` — eval suite for prompt/model regression testing

---

## Project layout

```
rocky/
├── main.py                        FastAPI server, OAuth, /api/chat, /dashboard, /metrics
├── agents/                        Multi-agent orchestration
│   ├── orchestrator.py              Router → specialist dispatch + greeting fast path
│   ├── router.py                    Routing decision (heuristic + LLM fallback)
│   ├── base.py                      Agent base class (LLM call + tracing + tool exec)
│   ├── email_agent.py               Gmail specialist
│   ├── calendar_agent.py            Calendar specialist
│   ├── web_agent.py                 Brave Search specialist
│   ├── memory_agent.py              save_fact / delete_fact specialist
│   ├── knowledge_agent.py           RAG email search specialist
│   └── _context.py                  Shared system-prompt context renderer
├── llm/
│   ├── minimax.py                   OpenAI-compatible client + cost calc + <think> stripping
│   ├── embedding.py                 Embedding abstraction (local ST / MiniMax / OpenAI providers)
│   └── t2a.py                       MiniMax speech-2.8-hd voice synthesis
├── tools/
│   ├── schemas.py                   14 OpenAI-format tool schemas
│   ├── registry.py                  Tool implementation wrappers (contextvars-threaded creds)
│   ├── _credentials.py              Per-request credential isolation
│   ├── brave_search.py              Brave AI-Grounding + fallback
│   ├── gmail_tools.py               Gmail HTTP API wrappers (unchanged from upstream)
│   └── calendar_tools.py            Calendar HTTP API wrappers (unchanged from upstream)
├── rag/
│   ├── email_indexer.py             Gmail backfill into vector store
│   └── email_store.py               numpy + JSON persistent vector index
├── tracing/tracer.py              Per-request Trace + Span model, ring buffer
├── metrics/cost.py                Aggregation across traces
├── prompts/v1/                    Versioned prompts (router + 5 specialists)
│   ├── router.md
│   ├── email.md  calendar.md  web.md  memory.md  knowledge.md
│   └── loader.py                  PROMPT_VERSION env → swap whole set
├── evals/
│   ├── test_cases.json              20 cases (routing + e2e)
│   └── run.py                       CLI runner with scorecard
├── dashboard.html                 Single-file live UI
├── auth.py                        Google OAuth credential management
├── database.py                    PostgreSQL multi-user store + Fernet token encryption
├── memory.py                      Long-term user memory (contacts + facts)
├── session.py                     2-hour TTL conversation history
└── requirements.txt
```

---

## Quick start

### 1. Clone, install

```bash
git clone <this repo>
cd rocky
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. Get API keys

| | Where | Note |
|---|---|---|
| **MiniMax** | [https://platform.minimax.io](https://platform.minimax.io) | Token Plan key (`sk-cp-...`) for chat. Optional pay-as-you-go key (`sk-api-...`) for T2A if your plan doesn't include speech. |
| **Brave Search** | [https://brave.com/search/api](https://brave.com/search/api) | Free tier 2K queries/month. |

### 3. Configure

```bash
cp .env.example .env
# Edit .env — at minimum set MINIMAX_API_KEY and BRAVE_API_KEY
```

For Gmail / Calendar (optional — chat / web / memory / knowledge work without it):
- Set up Google Cloud OAuth (consent screen, credentials, etc. — see upstream README)
- Either drop `token.json` for legacy single-user mode, or use `/login` for multi-user OAuth

### 4. Run

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

- **Dashboard**: http://localhost:8000/dashboard — try messages directly
- **Health**: http://localhost:8000/health
- **OAuth signup**: http://localhost:8000/login

### 5. Eval suite

```bash
python -m evals.run                  # all 20 cases
python -m evals.run --kind route_only  # routing only (fast, cheap)
python -m evals.run --case e2e-web-weather  # one case
```

---

## Key technical decisions

### 1. Router with heuristic short-circuit (`agents/router.py`)

Most requests hit a regex-classified heuristic that picks the route in **0ms with 0 tokens**. Only ambiguous messages cost a router LLM call. Measured on the eval suite: 7/9 routing decisions skip the LLM entirely.

### 2. OpenAI-compatible MiniMax client (`llm/minimax.py`)

MiniMax exposes an OpenAI-compatible endpoint at `https://api.minimax.io/v1`. We use the `openai` SDK with `base_url` override — gives us function calling, message format, and tool schemas for free. M2.7 is a reasoning model emitting `<think>...</think>` blocks; `strip_think()` removes them from user-facing content but preserves them in traces for debugging.

### 3. Per-request credentials via `contextvars` (`tools/_credentials.py`)

Each `/api/chat` request sets the current user's Google credentials in a `ContextVar`; tool functions read from there. No need to thread `credentials=` through every agent and tool.

### 4. Versioned prompts (`prompts/v1/*.md`)

Six markdown files, one per agent. The `prompts/loader.py` substitutes a `{context}` block (current time + user memory) into each. Set `PROMPT_VERSION=v2` in `.env` to swap to a new set without code changes — useful for prompt A/B without redeploys.

### 5. Two MiniMax keys for two billing models

Token Plan keys (`sk-cp-...`) cover text models cheaply under subscription quota. Speech models are usually pay-as-you-go (`sk-api-...`). `MINIMAX_T2A_API_KEY` env var lets you split them — chat goes through Token Plan, T2A through pay-as-you-go.

### 6. Vector store: numpy over Chroma

For ≤2K emails per user, a flat cosine search over a numpy matrix is sub-millisecond and has zero compile dependencies. Chroma's tokenizer dependency requires Rust on Python 3.14. The `rag/email_store.py` interface is kept generic — swap to Chroma / FAISS later if scale demands.

### 7. Graceful degradation across external dependencies

Each external dependency (MiniMax chat, MiniMax T2A, Brave, Gmail, Calendar) fails independently. Failures are caught and logged; the user gets at minimum a text reply. Examples:
- T2A fails → `audio_url=null`, iOS plays Siri TTS
- Brave fails → web_agent returns "couldn't reach search service"
- Gmail expired → email_agent surfaces a polite re-auth prompt

### 8. Tracing as a first-class primitive (`tracing/tracer.py`)

Every `/api/chat` opens a Trace; every LLM call, tool invocation, and routing decision is a Span. Stored in a 200-trace ring buffer in memory (no DB dependency). The dashboard polls `/traces` every 2s for live updates. Each span records `cost_usd`, `tokens`, `latency_ms`, and tool-specific metadata.

---

## Eval results (last run)

```
============================================================
  Eval scorecard — 20/20 passed (100%)
============================================================
  route_only   9/9    avg   525ms  cost $0.00044
  end_to_end   11/11  avg  2922ms  cost $0.00491
  overall      20/20  avg  1843ms  cost $0.00535  tokens 12,973
```

- **Routing accuracy: 100%** (heuristic short-circuit handles 78% of cases at zero cost)
- **End-to-end pass rate: 100%** across web search / memory / smalltalk / greeting paths
- **Average latency: 1.8s** (3× faster than the original baseline — improvements from M2.7 server-side optimisation + heuristic router rewrite)

Run `python -m evals.run` to reproduce.

---

## Tech stack

- **LLM**: MiniMax-M2.7 (released 2026-03-18, $0.30/$1.20 per 1M in/out)
- **Embeddings**: sentence-transformers `all-MiniLM-L6-v2` (384-dim, on-device, default); pluggable to MiniMax `embo-02` or OpenAI `text-embedding-3-small` via `EMBEDDING_PROVIDER`
- **Voice**: MiniMax speech-2.8-hd (with voice cloning support; voice_id forward-compatible from speech-02-hd)
- **Search**: Brave Search API (AI-Grounding endpoint)
- **Backend**: FastAPI + Uvicorn (Python 3.12+)
- **Auth**: Google OAuth2 (Gmail.modify + Calendar scopes), Fernet-encrypted refresh tokens
- **Storage**: PostgreSQL (users, encrypted tokens), local JSON (memory), numpy (RAG)
- **Front-end**: iOS Shortcut for voice, single-file HTML dashboard for ops

---

## Roadmap (deferred from this iteration)

- **Streaming** — stream M2.7 token-by-token into T2A so the user hears the start of the reply within ~1s (currently ~3s end-to-end).
- **Lighter router model** — switch the LLM fallback from a reasoning model to a smaller non-reasoning one to speed up the ambiguous-routing path. Heuristic short-circuit already covers 78% of requests at zero cost; this targets the remaining 22%.
- **More RAG sources** — extend the indexer to Notion / Google Drive / Slack via OAuth. The vector store is source-agnostic; each new source is roughly a 30-line indexer plus OAuth wiring.
- **Multimodal input** — accept photo + voice once MiniMax-VL-01 is exposed on the public chat-completions endpoint; in the interim, an OpenAI or Anthropic vision provider can sit behind the same interface.
- **Self-hostable Mac app** — bundle Rocky as a `.app` so privacy-conscious users can run it fully on-device. The multi-tenant code already supports per-user isolation, so collapsing it to single-user is straightforward.

---

## License

MIT — same as upstream.
