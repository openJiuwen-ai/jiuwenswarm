# Coding Memory

Coding Memory is a dedicated memory system for Code mode that allows the Agent to read, write, and edit persistent code context during coding tasks.

---

## Enabling

Coding Memory is automatically enabled when switching to Code mode — no additional configuration required. Related settings are in the `modes.code` section of `config/config.yaml`:

```yaml
modes:
  code:
    rails:
      - FileSystemRail
      - SkillUseRail
      - LspRail
    tools:
      - web_free_search
      - web_fetch_webpage
      - web_paid_search
      - user_todos
    embedding_config:
      model_name: null          # Empty = use global embedding config
      base_url: null
      api_key: null
```

---

## Tools

Coding Memory provides three tools, automatically registered by `CodingMemoryRail` within the subprocess:

### coding_memory_read

Read current coding memory content.

| Parameter | Type | Description |
|-----------|------|-------------|
| `query` | string | Search keyword to retrieve relevant coding context |

Returns matching code snippets and context descriptions.

### coding_memory_write

Write new code context or experience to coding memory.

| Parameter | Type | Description |
|-----------|------|-------------|
| `content` | string | Code content or experience to write |
| `section` | string | Category label (e.g. `architecture`, `bugfix`, `pattern`) |

### coding_memory_edit

Edit an existing coding memory entry.

| Parameter | Type | Description |
|-----------|------|-------------|
| `memory_id` | string | ID of the memory entry to edit |
| `content` | string | New content |
| `section` | string | Optional; change the category label |

---

## Relationship to Other Memory Systems

| Memory System | Applicable Modes | Description |
|--------------|-----------------|-------------|
| Built-in memory (`write_memory` / `read_memory`) | Agent mode | General conversation memory |
| Experience memory (`experience_retrieve` / `experience_learn`) | All modes | Task-level experience retrieval and consolidation |
| Coding memory (`coding_memory_read` / `write` / `edit`) | Code mode | Code context and coding experience |

All three memory systems can coexist in Code mode.

---

## Storage

Coding memory data is stored in the `workspace/agent/memory/` directory, sharing the storage path with built-in memory.

---

## Sleep-Time Consolidation (Dreaming)

When Dreaming is enabled in Code mode, the system reviews past coding sessions during idle time and automatically extracts reusable experience — debugging root causes, API edge behaviors, design decisions — to `{workspace}/coding_memory/consolidated_{hash}.md`. This is the sleep-time complement to the in-session `coding_memory_*` tools above.

See [Memory → Dreaming: Sleep-Time Memory Consolidation](Memory.md#dreaming-sleep-time-memory-consolidation) for how to enable it and extraction details.

---

## See Also

- [Modes](Modes.md) — Mode configuration and switching
- [Memory](Memory.md) — Built-in memory and external memory
- [Task Memory](TaskMemory.md) — Task-level experience retrieval