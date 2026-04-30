# Rocky —— 基于 MiniMax 的多 Agent 语音助手

🌐 [English](README.md) · **中文**

> **"Hi Rocky, 我明天有什么安排？"** —— "明天 10 点和 Wei 的 engineering review，下午 2 点和 Sarah 通合同电话。"（用我的克隆声音念出）
> **"上个月 Sarah 跟我说的合同那件事是什么？"** —— "Sarah 3 月 12 号邮件提到要把 Q2 续约价格往上调 8%。"（毫秒级 RAG 命中）
> **"周五下午 4 点加个 engineering review，跟 Wei 一起。"** —— "好的，已加到周五下午 4 点。"
> **"回复 Sarah 我 3 点到。"** —— "已发出。"（自动接在原邮件对话串下）

Rocky 和其他产品的对比：

- **Siri**：iOS 语音交互 + Apple Intelligence，但端侧 LLM 是小模型，agent 模型封闭，用户无法添加自己的 specialist 和 SaaS 信息源。
- **ChatGPT**：强大的多模态互动，但无法读取 Gmail / 日历 / 个人 SaaS 数据。
- **OpenClaw**：云端 LLM + 本地数据，但暂不支持 iOS 语音交互。
- **Rocky** ：iOS 原生语音交互、**MiniMax-M2.7** 驱动 5 个 tool-calling specialist、可读写用户 Gmail / 日历 / 永久记忆，支持网络搜索和 RAG 检索，用户可通过 MiniMax 语音克隆输出自己的声音。

**多租户架构**：每个用户配置自己的 MiniMax + Brave Search API keys（BYOK），所有凭据 Fernet 加密存 PostgreSQL，按请求通过 Python ContextVars 隔离。Google OAuth 在 `/login` 登录，`/settings` 配 keys，然后从 iOS Shortcut 或实时 dashboard 使用 Rocky。

---

## 亮点

| | Rocky |
|---|---|
| LLM | **MiniMax-M2.7**（OpenAI 兼容 API）|
| Web 搜索 | **Brave Search API**（独立索引、AI-Grounding endpoint）|
| 架构 | **Router → 5 个 specialist agent**（email、calendar、web、memory、knowledge）|
| Tool 注册 | **OpenAI 格式 tool schemas 版本化**（`tools/schemas.py`）|
| Prompts | **6 个版本化 prompt**（`prompts/v1/*.md`，通过 `PROMPT_VERSION` 环境变量切换）|
| RAG | **本地向量库**（sentence-transformers + numpy）默认覆盖近 6 个月邮件（Gmail OAuth 拉取）。存储层与数据源解耦 —— 接入 OAuth 类云端源（Notion / GDrive / Slack）只需新增约 30 行 indexer + OAuth 配置；本地文件源则需要额外的上传接口或独立的同步进程 |
| 语音 | **MiniMax speech-2.8-hd** T2A + 声音克隆（返回 `audio_url`）；BYOK 拒绝提示音一次性预合成、所有用户共用 |
| 多租户 | **每用户独立 MiniMax / Brave keys**（BYOK），Fernet 加密存 PG，ContextVar 按请求注入；operator 白名单允许可信用户跳过 BYOK，复用服务端共享额度 |
| 可观测性 | **每请求一个 trace**（含 BYOK 拒绝事件）、`/metrics`、实时 `/dashboard` |
| Eval | **20 个测试用例**的回归 suite，覆盖路由 / 工具调用 / latency / cost 指标 |

---

## 架构

```
                   iPhone — "Hi Rocky" 语音触发
                                    ↓
                        POST /api/chat  {message, tts: true}
                                    ↓
                          认证 + Session（PG，多用户）
                                    ↓
                       ┌────────── Router ──────────┐
                       │  agents/router.py          │
                       │  Heuristic 优先（0¢/0ms）  │
                       │  LLM 兜底（M2.7、JSON）     │
                       └────────────┬───────────────┘
                                    │
   ┌────────────┬────────────┬──────┴──────┬─────────────┬───────────────┐
   ↓            ↓            ↓             ↓             ↓               ↓
 Greeting    Email        Calendar       Web           Memory        Knowledge
 FastPath    Agent        Agent          Agent         Agent          (RAG)
 (无 LLM)    │            │              │             │              │
             Gmail API    Calendar API   Brave 搜索    用户记忆       numpy 向量库
             5 工具       5 工具         AI Grounding  保存/删除      本地向量检索
                                    │
                                    ↓ 最终回复文本
                       ┌─── MiniMax speech-2.8-hd T2A ───┐
                       │  llm/t2a.py                   │
                       │  返回 audio_url               │
                       └───────────────────────────────┘
                                    ↓
              { reply, audio_url, route, cost_usd, trace_id }
                                    ↓
              iOS Shortcut 播放 audio_url + 循环
```

**数据流接口：**
- `POST /api/chat` —— 语音命令进，结构化回复 + 音频出
- `GET /audio/{id}` —— 返回生成的 mp3（1 小时 TTL，自动清理）
- `GET /dashboard` —— 实时可观测 UI（KPI、route 分布、trace 钻取）
- `GET /metrics` —— Prometheus 风格的 JSON 聚合数据
- `GET /trace/{id}` —— 单请求的 span 树
- `GET /login` → `/setup` —— Google OAuth 多用户注册
- `python -m evals.run` —— prompt / 模型回归测试 eval suite

---

## 项目结构

```
rocky/
├── main.py                        FastAPI 服务器、OAuth、/api/chat、/dashboard、/metrics
├── agents/                        多 agent 编排
│   ├── orchestrator.py              Router → specialist 派发 + greeting 快路径
│   ├── router.py                    路由决策（heuristic + LLM 兜底）
│   ├── base.py                      Agent 基类（LLM 调用 + tracing + tool 执行）
│   ├── email_agent.py               Gmail specialist
│   ├── calendar_agent.py            Calendar specialist
│   ├── web_agent.py                 Brave 搜索 specialist
│   ├── memory_agent.py              save_fact / delete_fact specialist
│   ├── knowledge_agent.py           RAG 邮件检索 specialist
│   └── _context.py                  共享 system prompt context 渲染器
├── llm/
│   ├── minimax.py                   OpenAI 兼容 client + 成本计算 + <think> 剥离
│   ├── embedding.py                 Embedding 抽象层（本地 ST / MiniMax / OpenAI 三种 provider）
│   └── t2a.py                       MiniMax speech-2.8-hd 语音合成
├── tools/
│   ├── schemas.py                   14 个 OpenAI 格式 tool schemas
│   ├── registry.py                  Tool 实现 wrapper（contextvars 注入凭据）
│   ├── _credentials.py              按请求隔离凭据
│   ├── brave_search.py              Brave AI-Grounding + 兜底
│   ├── gmail_tools.py               Gmail HTTP API wrapper（沿用上游）
│   └── calendar_tools.py            Calendar HTTP API wrapper（沿用上游）
├── rag/
│   ├── email_indexer.py             Gmail backfill 进向量库
│   └── email_store.py               numpy + JSON 持久化向量索引
├── tracing/tracer.py              按请求的 Trace + Span 模型，环形缓冲
├── metrics/cost.py                跨 trace 聚合
├── prompts/v1/                    版本化 prompts（router + 5 specialists）
│   ├── router.md
│   ├── email.md  calendar.md  web.md  memory.md  knowledge.md
│   └── loader.py                  PROMPT_VERSION 环境变量 → 整套切换
├── evals/
│   ├── test_cases.json              20 case（路由 + e2e）
│   └── run.py                       带 scorecard 的 CLI runner
├── dashboard.html                 单文件实时 UI
├── auth.py                        Google OAuth 凭据管理
├── database.py                    PostgreSQL 多用户存储 + Fernet token 加密
├── memory.py                      长期用户记忆（联系人 + facts）
├── session.py                     2 小时 TTL 对话历史
└── requirements.txt
```

---

## 快速开始

### 1. 克隆、安装

```bash
git clone <this repo>
cd rocky
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

### 2. 申请 API key

| | 在哪里申请 | 备注 |
|---|---|---|
| **MiniMax** | [https://platform.minimax.io](https://platform.minimax.io) | Token Plan key（`sk-cp-...`）用于 chat。如果 plan 不包含语音，可选 pay-as-you-go key（`sk-api-...`）用于 T2A |
| **Brave Search** | [https://brave.com/search/api](https://brave.com/search/api) | 免费层 2K queries/月 |

### 3. 配置

```bash
cp .env.example .env
# 编辑 .env —— 至少设置 MINIMAX_API_KEY 和 BRAVE_API_KEY
```

Gmail / Calendar 配置（可选 —— chat / web / memory / knowledge 都不需要这一步）：
- 配置 Google Cloud OAuth（consent screen、credentials 等 —— 见上游 README）
- 要么放一个 `token.json` 走 legacy 单用户模式，要么用 `/login` 走多用户 OAuth

### 4. 运行

```bash
uvicorn main:app --host 0.0.0.0 --port 8000
```

- **Dashboard**: http://localhost:8000/dashboard —— 直接发消息测试
- **Health**: http://localhost:8000/health
- **OAuth 注册**: http://localhost:8000/login

### 5. Eval suite

```bash
python -m evals.run                  # 全部 20 case
python -m evals.run --kind route_only  # 只跑路由（快、便宜）
python -m evals.run --case e2e-web-weather  # 单个 case
```

---

## 关键技术决策

### 1. Router 的 heuristic 短路（`agents/router.py`）

大部分请求被 regex 分类的 heuristic 命中，**0ms、0 token** 决定路由。只有歧义消息才付一次 router LLM 调用的成本。Eval suite 实测：9 个路由决策中 7 个完全跳过了 LLM。

### 2. OpenAI 兼容的 MiniMax client（`llm/minimax.py`）

MiniMax 在 `https://api.minimax.io/v1` 暴露 OpenAI 兼容 endpoint。我们用 `openai` SDK 加 `base_url` override —— function calling、消息格式、tool schemas 直接复用，无需重写客户端。M2.7 是推理模型，输出包含 `<think>...</think>` 块；`strip_think()` 把它从用户可见内容里剔除，但保留在 trace 里供调试。

### 3. 通过 `contextvars` 实现按请求凭据隔离（`tools/_credentials.py`）

每个 `/api/chat` 请求把当前用户的 Google credentials 设到 ContextVar 里；tool 函数从这里读。**不需要把 `credentials=` 一路 prop drill 到每个 agent 和 tool**。

### 4. 版本化 prompts（`prompts/v1/*.md`）

6 个 markdown 文件，每个 agent 一个。`prompts/loader.py` 把 `{context}` 块（当前时间 + 用户记忆）替换进去。`.env` 里设 `PROMPT_VERSION=v2` 就能整套切换 —— 适合做 prompt A/B 测试，无需重新部署。

### 5. 两个 MiniMax key 对应两种计费模型

Token Plan key（`sk-cp-...`）在订阅配额下廉价覆盖文本模型。语音模型通常按 pay-as-you-go 收费（`sk-api-...`）。`MINIMAX_T2A_API_KEY` 环境变量让你把它们分开 —— chat 走 Token Plan，T2A 走 pay-as-you-go。

### 6. 向量库选择：numpy 而非 Chroma

每用户 ≤2K 邮件这个量级，在 numpy 矩阵上做 flat cosine 搜索是亚毫秒级、零编译依赖。Chroma 的 tokenizer 依赖在 Python 3.14 上需要 Rust toolchain。`rag/email_store.py` 接口刻意保持通用 —— 规模上来后换 Chroma / FAISS 是一行 import 的事。

### 7. 优雅降级架构

每个外部依赖（MiniMax chat、MiniMax T2A、Brave、Gmail、Calendar）独立失败。失败被捕获并记录；用户最差也能拿到一段文字回复。例子：
- T2A 失败 → `audio_url=null`，iOS 退化到 Siri TTS
- Brave 失败 → web_agent 返回"搜不到"
- Gmail 过期 → email_agent 提示重新授权

### 8. Tracing 一等公民（`tracing/tracer.py`）

每个 `/api/chat` 开一个 Trace；每个 LLM 调用、tool 调用、路由决策都是一个 Span。存 200-trace 的内存环形缓冲（无 DB 依赖）。Dashboard 每 2 秒 poll `/traces` 拿实时更新。每个 span 记录 `cost_usd`、`tokens`、`latency_ms`、以及工具特定的元数据。

---

## Eval 结果（最近一次运行）

```
============================================================
  Eval scorecard — 20/20 passed (100%)
============================================================
  route_only   9/9    avg   525ms  cost $0.00044
  end_to_end   11/11  avg  2922ms  cost $0.00491
  overall      20/20  avg  1843ms  cost $0.00535  tokens 12,973
```

- **路由准确率：100%**（heuristic 短路覆盖 78% case 在零成本）
- **端到端通过率：100%** 覆盖 web 搜索 / 记忆 / 闲聊 / greeting 路径
- **平均 latency：1.8 秒**（比初版 baseline 快 3 倍 —— 主要来自 M2.7 服务端优化 + heuristic router 重写）

跑 `python -m evals.run` 复现。

---

## 技术栈

- **LLM**：MiniMax-M2.7（2026-03-18 发布，$0.30 / $1.20 per 1M tokens 输入/输出）
- **Embedding**：sentence-transformers `all-MiniLM-L6-v2`（384 维，端侧推理，默认）；可通过 `EMBEDDING_PROVIDER` 切换到 MiniMax `embo-02` 或 OpenAI `text-embedding-3-small`
- **语音**：MiniMax speech-2.8-hd（支持声音克隆；voice_id 从 speech-02-hd 向前兼容）
- **搜索**：Brave Search API（AI-Grounding endpoint）
- **后端**：FastAPI + Uvicorn（Python 3.12+）
- **认证**：Google OAuth2（Gmail.modify + Calendar scopes）、Fernet 加密的 refresh token
- **存储**：PostgreSQL（用户、加密 token）、本地 JSON（记忆）、numpy（RAG）
- **前端**：iOS Shortcut 做语音入口、单文件 HTML dashboard 做运维

---

## Roadmap（本轮暂未实现的功能）

- **流式输出** —— M2.7 token-by-token 流到 T2A，让用户 1 秒内听到第一个字（当前端到端约 3 秒）。
- **更轻量的 router 模型** —— 把 router 的 LLM 兜底从推理模型换成非推理模型，加速歧义路由的兜底路径。Heuristic 短路已经覆盖 78% 请求，进一步优化的是剩下那 22%。
- **更多 RAG 数据源** —— 把 indexer 扩展到 Notion / Google Drive / Slack（OAuth）。向量库与数据源解耦，每个新源约 30 行 indexer + OAuth 接入。
- **多模态对话** —— 等 MiniMax-VL-01 通过公开的 chat-completions endpoint 上线后支持 photo + voice 输入；过渡期可临时接入 OpenAI / Anthropic 的 vision provider。
- **可自托管的 Mac App** —— 把 Rocky 打包成 `.app`，让重视隐私的用户本地运行、数据完全不出端。多租户代码已经支持按用户隔离，降级到单用户场景非常简单。

---

## License

MIT —— 沿用上游。
