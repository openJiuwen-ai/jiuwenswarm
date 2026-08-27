# JiuwenMemory (SDK Access)

> **Goal**: Help beginners configure JiuwenSwarm to access the agent-memory engine in **SDK mode**, from scratch to a working setup.
>
> **中文版**: [JiuwenMemory（SDK 接入）](../zh/JiuwenMemory-SDK接入.md)

---

## 1. Overview

### 1.1 What is JiuwenMemory

JiuwenMemory is an **external memory provider** supported by JiuwenSwarm, powered by the agent-memory (mem2.0 kernel) engine. It writes key information from conversations to structured storage and provides semantic recall on demand — giving the Agent persistent, cross-session memory.

JiuwenMemory offers two access modes:

| Mode | How it works | Best for |
|------|--------------|----------|
| `server` | Remote HTTP calls to an agent-memory server (`POST /v1/<verb>`) | Production, multi-client shared memory service |
| `sdk` | **Assembles the agent-memory kernel in-process** within JiuwenSwarm, direct calls, no HTTP hop | Single-machine embedding, no extra HTTP service, latency-sensitive |

Both modes end up at the same `MemoryAPI` (`write` / `recall`), with identical semantics: `add` ≡ `api.write`, `search` ≡ `api.recall`. The upper layer (Agent, memory rail) is mode-agnostic.

> This doc covers **SDK mode only**. For server mode, see [Memory](Memory.md).

### 1.2 Why SDK mode

- **No extra service**: No need to start a separate agent-memory HTTP server — the kernel runs in-process with JiuwenSwarm.
- **Direct backend access**: Skips the HTTP layer, shorter call path, lower latency.
- **Config convergence**: LLM and Embedding models **reuse JiuwenSwarm's top-level config** — no duplicate keys on the memory side.
- **Consistent semantics**: Exposes the same two tools (`mem2_search` / `mem2_add`) and the same system-prompt block as server mode; switching is invisible to the Agent.

### 1.3 How it works

```
┌──────────────── JiuwenSwarm process ──────────────┐
│  config.yaml                                       │
│    memory.engine: external        ← master switch  │
│    memory.external.provider: jiuwenmemory         │
│    memory.external.jiuwen.mode: sdk ← pick SDK     │
│  JiuwenMemoryProvider (mode=sdk)                  │
│    └─ assemble(config)  ← in-process kernel        │
│         ├─ api.write  ≡ write memory              │
│         └─ api.recall ≡ search memory              │
└─────────────────────┬──────────────────────────────┘
                      │ direct (no HTTP)
        ┌─────────────┼─────────────┐
        ↓             ↓             ↓
   ┌─────────┐  ┌──────────┐  ┌──────────────┐
   │ KV store │  │ Vector   │  │ Full-text    │
   │ Redis    │  │ Milvus   │  │ Elasticsearch│
   └─────────┘  └──────────┘  └──────────────┘
```

SDK mode auto-assembles the memory kernel in-process. The three backends answer "where to store" and "how to query": KV holds raw entries, vector index does semantic search, full-text does keyword matching — the three combine for the final result.

> 💡 Don't worry if the above is unclear — for daily use you only need to fill in the config; JiuwenSwarm handles the rest automatically.

---

## 2. Built-in Memory vs. JiuwenMemory

JiuwenSwarm defaults to **built-in memory** (`memory.engine: builtin`), based on local Markdown files; switching to JiuwenMemory uses remote structured backends. The key difference is not "tool names" but **how memory is triggered**:

- **Built-in memory**: No auto-trigger. The Agent must **actively call** `memory_*` tools (`memory_search` / `write_memory`, etc.) to read or write memory — nothing happens if it doesn't call them.
- **JiuwenMemory**: Driven automatically by the **ExternalMemoryRail (memory rail)**. At the start of each turn it auto-retrieves relevant memory and injects it into context; at the end of each turn it auto-persists the turn — **not necessarily calling any tool**, all outside the Agent's view.

| Aspect | Built-in (builtin) | JiuwenMemory (SDK) |
|--------|--------------------|---------------------|
| Storage | Local Markdown files (`MEMORY.md` / `USER.md` / daily logs) | Remote backends (Redis / Milvus / Elasticsearch) |
| Trigger | **Agent calls tools**, no automation | **Rail auto-triggers** (prefetch + sync_turn); tools optional |
| Turn start | No automatic action | Rail auto-`prefetch`, injects hits into context |
| Turn end | No automatic action | Rail auto-`sync_turn`, persists the turn (with breaker) |
| Agent-callable tools | `memory_search` / `write_memory` / `edit_memory` / `read_memory` etc. | `mem2_search` / `mem2_add` etc. (optional, as a supplement) |
| Extraction/dedup | Via Dreaming offline, or manual Agent summarization | `infer_turns: true` auto-distills into deduped facts via LLM |
| Data visibility | User can open `memory/` and read/edit Markdown directly | Data in remote backends, not directly viewable, recalled via tools |

### 2.1 JiuwenMemory Rail auto-trigger flow

Once ExternalMemoryRail is mounted, each turn runs these three steps (invisible to the user):

```
before_invoke (turn start)
  └─ initialize provider on first call (once only)

before_model_call (before LLM call)
  └─ prefetch(user's current query)        ← auto-retrieve memory
       5s timeout; skipped on failure / no hits
  └─ hits wrapped in a <memory-context> block, injected into context
       (tagged "recalled memory, NOT new user input" to avoid confusion)

after_invoke (turn end)
  └─ sync_turn(user query, assistant output)  ← auto-persist the turn
       serialized execution + breaker (5 consecutive failures → 120s cooldown)
       skipped on background runs (heartbeat/cron)
```

The injected memory block looks like this (the Agent sees it but knows it's recalled memory, not new user input):

```xml
<memory-context>
[System note: recalled memory context from long-term memory, NOT new user input.]

- User likes spicy food
- User is based in Shanghai
</memory-context>
```

> 💡 So with JiuwenMemory, **even if the Agent calls no `mem2_*` tool in a turn, retrieval and persistence still happen** — that's the rail auto-driving. The tools (`mem2_search` / `mem2_add`) only give the Agent an optional, explicit way to search/write; not calling them is fine.

### 2.2 How to tell which one is active

Look at the **behavior**, not just tool names:

- **Built-in memory**: you'll see the Agent **actively call** `memory_search` / `write_memory` / `edit_memory` / `read_memory` etc., and `memory/` Markdown files change. No call = no memory behavior.
- **JiuwenMemory**: a `<memory-context>` block appears in context (auto, each turn), but there may be **no** `mem2_*` tool calls — persistence is done by the rail. If you do see `mem2_*` calls, that's an explicit supplement the Agent chose to make.

Same request ("remember I like spicy food"), very different persistence paths:

```
# Built-in: the Agent must actively call a tool
[tool: write_memory]  path=memory/USER.md, content=...likes spicy food...
(no call → nothing stored)

# JiuwenMemory: rail auto-persists at turn end, Agent is unaware
(no tool call needed; sync_turn auto-distills "likes spicy food" into storage)
(optional: Agent may also call [tool: mem2_add] for an explicit write)
```

> 💡 Both can be used independently, or together via `memory.engine: both` — then built-in `memory_*` tools and the external rail auto-trigger coexist. This doc covers switching to JiuwenMemory (`external`).

---

## 3. Prerequisites

### 3.1 The main app works

JiuwenSwarm is installed and can hold a normal conversation. Memory is a capability layered on top of conversation, so the main app must run first. See [Install Guide](InstallGuide.md) if not installed.

### 3.2 Install the agent-memory kernel

```bash
pip install JiuwenMemory
```

Verify:

```bash
python -c "from api import assemble; print('agent-memory OK')"
```

> ⚠️ If missing or installed in the wrong env, startup logs show a `RuntimeError` prompting `pip install JiuwenMemory` and the rail won't mount (main flow is not blocked). If you launch JiuwenSwarm with `uv`/a venv, run `pip install` in the **same environment**.

### 3.3 Deploy three backend services

SDK mode needs three backends (KV / vector / full-text); deploy them and confirm addresses are reachable:

| Backend | Purpose | Implementation |
|---------|---------|----------------|
| KV store | Store raw memory entries | Redis |
| Vector index | Semantic search (by meaning) | Milvus |
| Full-text | Keyword matching (by words) | Elasticsearch |

> All three are remote services — reliable and suitable for long-term use. Their `type`/`url` go in the config in §4.2.

---

## 4. Configuration (two steps)

> Find `config.yaml` (usually at `~/.jiuwenswarm/config/config.yaml`). Two steps only.

### 4.1 Step 1: Confirm the LLM and Embedding config

**This is the most overlooked step.** In SDK mode, the memory's LLM extraction and Embedding vectorization **reuse JiuwenSwarm's top-level config** — not duplicated under `jiuwen`:

- **LLM**: distills conversations into deduped facts when `infer_turns: true`. Sourced from the `is_default: true` entry in top-level `models.defaults`.
- **Embedding**: turns text into vectors for semantic search. Sourced from the top-level `embed` section.

If you can chat normally the LLM is basically configured, but confirm **Embedding is also set** (many people configure only the chat model). Confirm these two sections exist at the top level (usually there by default):

```yaml
models:
  enable_free_models: true
  defaults:
    - model_client_config:
        api_base: ${API_BASE}
        api_key: ${API_KEY}
        model_name: ${MODEL_NAME}
        client_provider: ${MODEL_PROVIDER}
      model_config_obj:
        temperature: 0.95
      is_default: true               # SDK mode uses this is_default model

embed:
  embed_api_key: ${EMBED_API_KEY}
  embed_base_url: ${EMBED_API_BASE}
  embed_model: ${EMBED_MODEL}
```

Corresponding env vars (in `.env` or the system environment):

```bash
# LLM (used for chat; SDK memory extraction reuses it too)
export API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
export API_KEY=sk-your-llm-key
export MODEL_NAME=qwen-plus
export MODEL_PROVIDER=openai

# Embedding (required for SDK semantic search; all three are mandatory)
export EMBED_API_KEY=sk-your-embed-key
export EMBED_API_BASE=https://dashscope.aliyuncs.com/compatible-mode/v1
export EMBED_MODEL=text-embedding-v3
```

> ⚠️ All three `EMBED_*` vars are mandatory. Configuring only `API_*` (chat works) without `EMBED_*` means memory search won't work.
>
> The Embedding model's output dim must match `embedder_dim` in §4.2; see §6.2.

### 4.2 Step 2: Configure config.yaml

Add this to the `memory` section (remote three-service setup):

```yaml
memory:
  engine: external                         # external only (both = built-in + external)
  external:
    provider: jiuwenmemory                # pick JiuwenMemory
    user_id: __default__                  # data isolation id (= mem2 scope)
    scope_id: __default__
    jiuwen:
      mode: sdk                           # key: use SDK mode
      tenant_id: default                  # org axis
      infer_turns: true                   # distill turns via LLM extraction + dedup
      save_assistant_turns: false         # default: store user turns only
      sdk:
        kv_type: redis
        kv_url: redis://localhost:6379/0        # your Redis address
        vector_type: milvus
        vector_url: http://localhost:19530      # your Milvus address
        db_type: elasticsearch
        db_url: http://localhost:9200           # your Elasticsearch address
        embedder_dim: 1024                      # must match vector DB dim & Embedding output
```

Replace the three `*_url` values with your actual service addresses.

> 💡 If you prefer env-var management (no direct `config.yaml` edits), every SDK field can be overridden by env vars. `config.yaml` only needs:
>
> ```yaml
> memory:
>   engine: external
>   external:
>     provider: jiuwenmemory
>     jiuwen:
>       mode: sdk
> ```
>
> The rest via env vars: `MEMORY_ENGINE` / `MEMORY_EXTERNAL_PROVIDER` / `JIUWEN_MEMORY_MODE` / `JIUWEN_KV_TYPE`+`JIUWEN_KV_URL` / `JIUWEN_VECTOR_TYPE`+`JIUWEN_VECTOR_URL` / `JIUWEN_DB_TYPE`+`JIUWEN_DB_URL` / `JIUWEN_EMBEDDER_DIM` (full list in §5.3).

---

## 5. Field Reference

### 5.1 Common fields (both modes)

| Field | Default | Meaning |
|-------|---------|---------|
| `mode` | `server` | `sdk` = in-process kernel |
| `tenant_id` | `default` | org axis of Scope, for data isolation |
| `user_id` | `__default__` (top-level `memory.external.user_id`) | user axis of Scope (= mem2 scope) |
| `infer_turns` | `true` | whether `sync_turn` distills user turns via LLM + dedup |
| `save_assistant_turns` | `false` | whether `sync_turn` also stores assistant turns (default: user turns only) |

> `user_id` is normally injected by the rail at `initialize()` from the top-level `memory.external.user_id`; `jiuwen.user_id` is an explicit override kept for parity with other providers.

### 5.2 SDK-only fields (`memory.external.jiuwen.sdk`)

Only the three backends' `type` + `url`; LLM/Embedder reuse the top level:

| Field | Default | Description |
|-------|---------|-------------|
| `kv_type` | `redis` | KV backend: `sqlite` / `redis` / `memory` |
| `kv_url` | `redis://localhost:6379/0` | `sqlite`=file path; `redis`=connection string |
| `vector_type` | `milvus` | vector backend: `milvus` / `memory` |
| `vector_url` | `http://localhost:19530` | Milvus service address |
| `db_type` | `elasticsearch` | full-text backend: `elasticsearch` / `memory` |
| `db_url` | `http://localhost:9200` | Elasticsearch address |
| `embedder_dim` | `1024` | vector dim; must match vector DB dim and Embedding output |

### 5.3 Env vars

| Variable | Description |
|----------|-------------|
| `MEMORY_ENGINE` | master switch: `external` / `both` / `builtin` / `none` |
| `MEMORY_EXTERNAL_PROVIDER` | provider name, pick `jiuwenmemory` |
| `MEMORY_USER_ID` | data isolation id |
| `MEMORY_SCOPE_ID` | scope id |
| `JIUWEN_MEMORY_MODE` | `server` / `sdk` |
| `JIUWEN_MEMORY_TENANT_ID` | tenant_id |
| `JIUWEN_KV_TYPE` / `JIUWEN_KV_URL` | KV backend type and address |
| `JIUWEN_VECTOR_TYPE` / `JIUWEN_VECTOR_URL` | vector backend type and address |
| `JIUWEN_DB_TYPE` / `JIUWEN_DB_URL` | full-text backend type and address |
| `JIUWEN_EMBEDDER_DIM` | vector dim |

### 5.4 Builder-pinned options

These are **pinned** by the builder when stitching the SDK config — not exposed in `config.yaml`, not configurable:

| Option | Value | Reason |
|--------|-------|--------|
| `rerank_enabled` | `false` | no external reranker |
| `graph_enabled` | `false` | no graph |
| `enable_thinking` | `false` | thinking chain off |
| `embedder_ssl_verify` | `false` | Embedding SSL verify off |
| `tokenizer.default.target` | `jieba` | Chinese BM25 tokenizer |

---

## 6. Backend Selection (Advanced)

### 6.1 Backend type/url reference

#### KV store (`kv_type` / `kv_url`)

| `kv_type` | `kv_url` format | Notes |
|-----------|-----------------|-------|
| `redis` | `redis://host:port/db` | remote Redis connection string |
| `sqlite` | file path (e.g. `./agent_memory.db`) | local SQLite file; falls back to `agent_memory.db` if a `redis://...` URL is given |
| `memory` | not needed | in-process dict, lost on restart |

#### Vector index (`vector_type` / `vector_url`)

| `vector_type` | `vector_url` format | Notes |
|---------------|----------------------|-------|
| `milvus` | `http://host:19530` | remote Milvus service address |
| `memory` | not needed | in-process brute-force cosine, small data only |

#### Full-text (`db_type` / `db_url`)

| `db_type` | `db_url` format | Notes |
|-----------|------------------|-------|
| `elasticsearch` | `http://host:9200` | remote Elasticsearch address |
| `memory` | not needed | in-process inverted index, lost on restart |

### 6.2 Vector dim reference

`embedder_dim` must satisfy both:

1. Match the Milvus collection `dim` (the builder writes `params.dim`)
2. Match the configured Embedding model's actual output dim

Common Embedding models and dims:

| Embedding model | Typical dim | Set `embedder_dim` to |
|-----------------|-------------|------------------------|
| Tongyi text-embedding-v3 | 1024 | `1024` |
| OpenAI text-embedding-3-small | 1536 | `1536` |
| OpenAI text-embedding-3-large | 3072 | `3072` |

> ⚠️ A mismatch causes write/search failures. If Milvus has an existing collection with a wrong dim, clear it before restarting.

---

## 7. Startup & Verification

### 7.1 Start JiuwenSwarm

Start it the way you normally do (CLI or desktop).

### 7.2 Check the mount succeeded

Check the logs (paths in [Logs](Logs.md)) for the key line:

```
[ExternalMemoryBuilder] JiuwenMemory provider built (mode=sdk, tenant=default)
```

This means the SDK memory rail was assembled successfully. **No such line = not mounted** — check §10.1.

### 7.3 Verify memory works

Test write and recall in a conversation:

```
User: Remember that I like spicy food
Assistant: Got it — noted that you like spicy food.
[mem2_add tool called, memory written]

# new session or a few turns later
User: Any dietary preferences for me?
Assistant: Based on memory, you like spicy food.
[mem2_search tool called, recalled relevant memory]
```

Seeing the Agent call `mem2_search` / `mem2_add` and recall info across sessions means it's working.

### 7.4 Self-check commands

If the mount failed, run these two first:

```bash
# 1. Confirm the agent-memory kernel is installed
python -c "from api import assemble; print('agent-memory OK')"

# 2. Confirm the top-level Embedding config (required for SDK)
python -c "import os; print('EMBED_API_KEY:', bool(os.environ.get('EMBED_API_KEY'))); print('EMBED_API_BASE:', os.environ.get('EMBED_API_BASE')); print('EMBED_MODEL:', os.environ.get('EMBED_MODEL'))"
```

All three `EMBED_*` must be set.

---

## 8. Usage

### 8.1 Agent auto-call (recommended)

Once configured, no manual action is needed. JiuwenMemory exposes **two tools** plus a system-prompt block, and the Agent calls them automatically during conversation:

| Tool | Purpose |
|------|---------|
| `mem2_search` | semantic memory recall by query |
| `mem2_add` | write a memory; `infer` controls whether LLM extraction/dedup runs |

Both tools are identical between SDK and server modes; the Agent is unaware.

### 8.2 Auto turn persistence (sync_turn)

At the end of each turn, the rail calls `sync_turn` to auto-persist the turn:

- `infer_turns: true` (default): user turn is **distilled into deduped facts** via LLM, avoiding redundancy.
- `infer_turns: false`: user turn is stored **verbatim**.
- `save_assistant_turns: true`: also stores the assistant turn (always verbatim, never extracted).

> 💡 Default `save_assistant_turns: false` — assistant replies are usually derivable from the user turn and low-value; storing only the user turn is more efficient. Set `true` to keep full conversation transcripts.

### 8.3 Tool-call return examples

`mem2_add` write returns:

```json
{
  "result": "stored",
  "item_id": "mem-abc123",
  "content": "User likes spicy food",
  "tier": "HOT"
}
```

- `result`: `stored` (new write) or `deduped` (judged a duplicate after infer, not added)
- `item_id`: empty means deduped, with `result` = `deduped`

`mem2_search` recall returns:

```json
{
  "results": [
    {"content": "User likes spicy food", "item_id": "mem-abc123", "score": 0.92}
  ],
  "count": 1
}
```

---

## 9. Access Pipeline (Advanced)

> For readers who want the internals; skippable for daily use.

### 9.1 Assembly stage

```
build_external_memory_rail(config)
  └─ _build_jiuwen_provider(ext_cfg, config)
       └─ mode == "sdk"
            ├─ _build_jiuwen_sdk_config_dict(sdk_cfg, full_config)
            │    ├─ kv_store      = _kv_spec(kv_type, kv_url)
            │    ├─ vector_store  = _vector_spec(vector_type, vector_url, dim)
            │    ├─ fulltext_store = _fulltext_spec(db_type, db_url)
            │    ├─ globals = {embedder_dim, chunk_size, rerank_off, ...}
            │    ├─ llm/embedder creds ← top-level models.defaults / embed
            │    └─ returns {globals, tokenizer, kv_store, vector_store, fulltext_store, [llm], [embedder]}
            └─ JiuwenMemoryProvider(mode=sdk, config_dict=..., tenant_id, user_id, ...)
```

### 9.2 Init and call stage

```
_SDKBackend.initialize()
  ├─ from api import assemble
  ├─ cfg = Config.from_dict(config_dict)   # empty dict / failure → default, warn only
  ├─ self._api = assemble(config=cfg)       # failure raises RuntimeError, caught by builder
  └─ caches Scope / Modality / Context / DisclosureLevel types

search(query, top_k):
  ├─ scope = Scope(org=tenant_id, user=user_id)
  ├─ ctx = Context(scope)
  └─ asyncio.to_thread(api.recall, query, ctx, identity=scope, top_k=k, disclosure=L2)
       # api.recall is sync, uses asyncio.run() internally → offloaded to a worker thread

add(content, infer, tags):
  ├─ metadata = {"infer": "true"} if infer else {}
  ├─ modality = Modality.TEXT
  └─ asyncio.to_thread(api.write, content, scope, modality, identity=scope, tags, metadata)
```

### 9.3 Failure-degradation principle

Any assembly/call failure **never blocks the main flow**:

- agent-memory not installed → `RuntimeError` caught by builder, rail returns `None`, chat continues.
- `Config.from_dict` fails → falls back to built-in default config, warn only.
- Single read/write exception → returns empty results (`search` → `[]`, `add` → `None`), no throw.

---

## 10. FAQ

### 10.1 Startup says "pip install JiuwenMemory"

**Problem**: logs show a `RuntimeError` prompting `pip install JiuwenMemory`; the rail didn't mount.

**Fix**: the agent-memory kernel isn't installed in the current Python env. Run `pip install JiuwenMemory`; verify with §7.4. **Most common cause**: launching JiuwenSwarm with `uv`/a venv but installing the kernel in system Python — they must be the same env.

### 10.2 Rail not mounted (no "built" log line)

**Problem**: configured `provider: jiuwenmemory`, but no `JiuwenMemory provider built` line in the logs.

**Checks**:

1. `memory.engine` is `external` or `both`, not the default `builtin`.
2. `memory.external.provider` is `jiuwenmemory` (lowercase, no spaces, no extra chars).
3. `jiuwen.mode` is `sdk`.
4. `pip install JiuwenMemory` ran in the right env (verify via §7.4).
5. Look for an earlier error in the logs (e.g. kernel assembly failure).

### 10.3 LLM extraction fails or memory isn't distilled

**Problem**: `infer_turns: true` but memory isn't distilled into facts — all verbatim.

**Fix**: in SDK mode the LLM reuses top-level `models.defaults[is_default=true]`. Confirm the top level has a working LLM (`api_base` / `api_key` / `model_name` all set) and the model is callable (normal chat usually means it's fine). When the LLM is unavailable, `infer` degrades — memory is still stored verbatim, not lost, just not deduped.

### 10.4 Semantic search not working / dim error

**Problem**: search returns nothing, or logs report a vector-dim mismatch.

**Fix**: `embedder_dim` must match both the Milvus collection dim and the Embedding model's output dim.

1. Confirm `EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL` are all set (verify via §7.4).
2. Per §6.2, check your Embedding model's actual output dim and adjust `embedder_dim`.
3. If Milvus has an existing collection with a wrong dim, clear it before restarting.

### 10.5 How data isolation works

**Answer**: JiuwenMemory isolates via `Scope(org=tenant_id, user=user_id)`:

- `tenant_id`: org axis, usually an org/tenant.
- `user_id`: user axis, i.e. the mem2 scope string, injected from top-level `memory.external.user_id`.

Different `(tenant_id, user_id)` pairs are mutually invisible. For multi-user scenarios, give each user a distinct `user_id`.

---

## 11. Quick Checklist

Follow this to complete setup:

- [ ] Main app JiuwenSwarm can chat normally (prerequisite)
- [ ] `pip install JiuwenMemory`, and verify with `python -c "from api import assemble"`
- [ ] Top-level `EMBED_API_KEY` / `EMBED_API_BASE` / `EMBED_MODEL` set (§4.1, required for SDK)
- [ ] Deploy Redis / Milvus / Elasticsearch and confirm addresses are reachable (§3.3)
- [ ] Fill the SDK config in the `memory` section of `config.yaml`; replace the three `*_url` with real addresses (§4.2)
- [ ] Align `embedder_dim` with your Embedding model's dim (§6.2)
- [ ] Start; logs show `JiuwenMemory provider built (mode=sdk, ...)` (§7.2)
- [ ] Conversation test: ask the Agent to remember something, ask again a few turns / a new session later (§7.3)

---

## Back to Navigation

- [Back to docs home](../README_EN.md)
- [Back to project home](../../README.md)
