# Task Memory (Experience Memory)

Task Memory is JiuwenSwarm's built-in experience system that allows the Agent to retrieve, record, and consolidate lessons from past tasks — avoiding repeated mistakes and reusing effective solutions in future tasks.

---

## Configuration

Configure in `config/config.yaml`:

```yaml
task_memory:
  enabled: true                          # Master switch
  retrieval_algo: ACE                    # Retrieval algorithm: ACE | ReasoningBank | ReMe
  summary_algo: ACE                      # Summary algorithm: ACE | ReasoningBank | ReMe
  llm_model: ${TASK_MEMORY_LLM_MODEL}    # LLM model for task memory (empty = main model)
  embedding_model: ${TASK_MEMORY_EMBED_MODEL}
  api_key: ${TASK_MEMORY_API_KEY}
  api_base: ${TASK_MEMORY_API_BASE}
```

When `enabled: true`, the Agent gains the three tools described below.

---

## Tools

### experience_retrieve

Retrieve relevant past experience based on a query.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Describe the current task or question; the system matches the most relevant experience |

Returns `memory_string` (readable text) and `retrieved_memory` (structured list).

**Recommended: call this at the start of every task** to check for reusable experience.

### experience_learn

Record a key finding, rule, or insight from the current task and consolidate it into the experience store.

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `params.content` | string | Yes | The experience content to record |
| `params.section` | string | No | Category label, default `general` |
| `params.when_to_use` | string | No | When to apply this experience |
| `params.title` | string | No | Experience title |
| `params.description` | string | No | Detailed description |
| `params.query` | string | No | Associated query |
| `params.label` | string | No | Custom label |
| `params.tools_used` | list | No | Tool call outcomes, e.g. `[{"tool": "web_search", "status": "success"}]` |

**Recommended: call once before the final reply.** Recording failed tool calls is especially valuable.

### experience_clear

Wipe all stored task memory from `task-data.json`.

> ⚠️ Only call when the user explicitly asks to clear. Always confirm first.

---

## Storage

Experience data is persisted in `workspace/agent/task-data.json`. Even when the external experience service is unavailable, local persisted data is still accessible (returns `persisted_only` status).

When the external service is healthy, retrieval results merge both local persisted data and service-side data.

---

## Retrieval Algorithms

| Algorithm | Description |
|-----------|-------------|
| **ACE** | Default algorithm; embedding-similarity-based retrieval |
| **ReasoningBank** | Reasoning-enhanced retrieval; suited for complex reasoning chains |
| **ReMe** | Reflection-based retrieval; focuses on learning from failures |

---

## Environment Variables

| Variable | Description |
|----------|-------------|
| `TASK_MEMORY_LLM_MODEL` | LLM model for task memory |
| `TASK_MEMORY_EMBED_MODEL` | Embedding model for task memory |
| `TASK_MEMORY_API_KEY` | API key for task memory |
| `TASK_MEMORY_API_BASE` | API base URL for task memory |

---

## See Also

- [Memory](Memory.md) — Built-in and external memory overview
- [Configuration](Configuration.md) — Full `task_memory` section configuration