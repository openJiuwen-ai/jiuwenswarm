# Memory

Memory gives JiuwenClaw **persistent, cross-session recall**: important facts are written to files and retrieved with semantic search (plus optional BM25).

## Configuration

Retrieval defaults to BM25 full-text search. Configure **`EMBED_API_KEY`** (and related embed settings) for vector + BM25 hybrid search.

| Variable | Description |
|----------|-------------|
| `EMBED_API_KEY` | Embedding API key (mock provider if unset) |
| `EMBED_API_BASE` | Embedding endpoint URL |
| `EMBED_MODEL` | Embedding model name |

![Memory config](../assets/images/memory_config.png)


## File layout

Memory is plain Markdown; the agent uses file tools:

```
{workspace_dir}/memory
├── MEMORY.md               # Long-term memory
├── USER.md                 # User profile
└── YYYY-MM-DD.md           # Daily log
```
![Memory files](../assets/images/memory_files.png)

### `memory/MEMORY.md` (long-term)

- **Use**: Decisions, preferences, stable facts.
- **Updates**: `write` / `edit` tools.

### `USER.md` (profile)

- **Use**: Name, role, hobbies, location, etc.
- **Updates**: `write` / `edit`.

### `YYYY-MM-DD.md` (daily)

- **Use**: Day log, running context.
- **Updates**: Append via `write` / `edit`; summarization may run when conversations are long.

## When writes happen

| Kind | Target | How | Example |
|------|--------|-----|---------|
| Decisions, preferences, facts | `memory/MEMORY.md` | write / edit | “Project uses Python 3.12” |
| Profile | `memory/USER.md` | write / edit | User name, job, interests |
| Daily notes | `memory/YYYY-MM-DD.md` | write / edit | “Shipped login fix today” |
| “Remember this” | `memory/YYYY-MM-DD.md` | write | User asks to remember a fact |

## Architecture overview

```
User / Agent
     │
     ▼
MemoryIndexManager
              ├── Persistence (file tools)
              ├── File watch (watchdog)
              ├── Semantic search (vector + BM25)
              └── File reads (on demand)
```

### Capabilities

| Capability | Description |
|------------|-------------|
| Persistence | Markdown files as source of truth |
| File watch | Watchdog updates local indexes async |
| Semantic search | Embeddings + BM25 hybrid recall |
| Direct read | Read specific files to keep context small |

### Technical stack

```
┌─────────────────────────────────────────────────────────────────┐
│                     MemoryIndexManager                          │
├─────────────────────────────────────────────────────────────────┤
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────────────┐      │
│  │ Config      │  │ Embedding   │  │ SQLite Database     │      │
│  │ (config.py) │  │ Provider    │  │ - chunks            │      │
│  └─────────────┘  └─────────────┘  │ - files             │      │
│         │                │         │ - embedding_cache   │      │
│         │                │         │ - chunks_fts (FTS5) │      │
│         │                │         │ - chunks_vec (vec0) │      │
│         │                │         └─────────────────────┘      │
│         │                │                   │                  │
│         ▼                ▼                   ▼                  │
│  ┌───────────────────────────────────────────────────────────┐  │
│  │                    Search Pipeline                        │  │
│  │  Query ──► Embed ──► Vector Search ──┐                    │  │
│  │                                      ├─► Merge ──► Results|  |
│  │  Query ──► FTS5 Search ──────────────┘                    │  │
│  └───────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
```

## Retrieval

### Modes

| Mode | When | Example |
|------|------|---------|
| Semantic search | Fuzzy intent, unknown file | “What did we decide about deploy?” |
| Direct read | Known date or path | Read `memory/2026-02-28.md` |

### Hybrid scoring

```
Query ──► Embed ──► Vector Search ──┐
                                    ├─► Merge ──► Results
Query ──► FTS5 Search ──────────────┘
```

Combined score: `score = vectorWeight * vectorScore + textWeight * textScore` (defaults: vector 0.7, text 0.3).
