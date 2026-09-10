# Memory

JiuwenSwarm uses external services for persistent, cross-session memory. The Celia provider injects the Memory prompt; configured `gausspdmcp` tools supply schemas and execution through the standard MCP integration. Project coding memory remains separate.

**External Memory Providers**, supporting third-party memory services (OpenJiuwen LTM, Mem0, OpenViking) or custom plugins.

---

## Engine Switch

Control memory engine mounting via `memory.engine` config:

| Value | Description |
|-------|-------------|
| `external` | External memory (default; requires a provider) |
| `none` | All disabled |

---

## Configuration

### External Memory

Config location: `memory.external` section in `config.yaml`.

```yaml
memory:
  engine: external
  external:
    provider: mem0   # Choose one: openjiuwen | mem0 | openviking | <plugin-name>
    user_id: __default__
    scope_id: __default__

    # Provider-specific config
    openjiuwen:
      kv_type: shelve
      vector_type: chroma
      db_type: sqlite
    mem0:
      api_key: ""
      user_id: ""
      agent_id: ""
      rerank: true
    openviking:
      endpoint: http://127.0.0.1:1933
      api_key: ""
      account: root
      user: default
```

#### Supported Providers

| Provider | Description | Required Config |
|----------|-------------|-----------------|
| `openjiuwen` | Local long-term memory (KV + Vector + DB) | None (uses default ~/.jiuwenswarm/memory/ltm) |
| `mem0` | Cloud fact extraction & semantic retrieval | `api_key` (from mem0.ai) |
| `openviking` | ByteDance context database | `endpoint`, `api_key` |
| `<plugin-name>` | Custom plugin | ~/.jiuwenswarm/plugins/memory/<name>/ |

#### External Memory Environment Variables

| Variable | Description |
|----------|-------------|
| `MEMORY_EXTERNAL_PROVIDER` | Provider name (overrides config.yaml) |
| `MEMORY_USER_ID` | Data isolation identifier |
| `MEMORY_SCOPE_ID` | Scope identifier |
| `MEM0_API_KEY` | Mem0 API key |
| `MEM0_USER_ID` | Mem0 user identifier |
| `OPENVIKING_ENDPOINT` | OpenViking service address |
| `OPENVIKING_API_KEY` | OpenViking API key |

### Dreaming Configuration

Dreaming is a sleep-time memory consolidation mechanism (see [Dreaming: Sleep-Time Memory Consolidation](#dreaming-sleep-time-memory-consolidation) below). Disabled by default — must be explicitly turned on.

```yaml
memory:
  dreaming:
    agent:
      enabled: false
      interval_seconds: 14400   # 4 hours by default
    code:
      enabled: false
      interval_seconds: 14400
```

Env overrides:

| Variable | Description |
|----------|-------------|
| `DREAMING_AGENT_ENABLED` | Toggle for agent-mode Dreaming |
| `DREAMING_CODE_ENABLED` | Toggle for code-mode Dreaming |
| `DREAMING_INTERVAL` | Sweep interval in seconds (applies to both modes) |

The LLM call reuses `models.default`; no extra model config required.

---

## Retired file memory

The built-in file-memory tools, index, daily-file migration and prompt injection have been removed. Workspace defaults no longer create `USER.md`, `memory/MEMORY.md` or `memory/daily_memory/*.md`. Existing files are left on disk and are not read by the memory integration. Daily reports no longer collect these files.

Project coding memory and team memory retain their own storage and tools.

## Dreaming: Sleep-Time Memory Consolidation

In addition to in-session writes by the agent, JiuwenSwarm provides **Dreaming**: a sleep-time mechanism that periodically scans past sessions during idle time, calls an LLM to extract content worth keeping long-term, and writes the result to persistent memory files. Agent and Code modes share the same pipeline.

| Mode | Extraction target | Output |
|------|-------------------|--------|
| `agent` | User preferences, background, areas of interest | `{workspace}/memory/DREAMING.md` (single file, max 50 entries, oldest evicted) |
| `code`  | Debugging root causes, API edge behaviors, design decisions, reusable engineering experience | `{workspace}/coding_memory/consolidated_{hash}.md` (one file per entry, dedup by content SHA256) |

For enabling, see [Configuration → Dreaming Configuration](#dreaming-configuration) above.

### How it works

- **Scheduling**: an in-process Orchestrator fires every `interval_seconds`, with a 120s initial delay after startup
- **Busy backoff**: skipped when the agent is actively handling a request; retried next cycle
- **Incremental scan**: a checkpoint tracks processed sessions; sessions with fewer than 4 rounds or older than 30 days are skipped
- **Cost control**: at most 10 sessions per sweep, each compressed to under 30K tokens; at most 5 entries extracted per session
- **Retry policy**: LLM-call failures are not checkpointed so the session is retried; JSON parse failures are skipped

Difference from the in-session writes above: those rely on the agent's real-time judgment about what to remember. Dreaming revisits whole conversations during idle time and decides afterwards — an offline consolidation channel.


## Agent Team Memory

In Agent Team mode, each team has two memory layers:

| Layer | Access | Writer |
|-------|--------|--------|
| Personal memory | Owned by a single member | The member itself (calls memory tools in-session) |
| Team memory | Read-only to all members | An extractor agent spawned by the Leader at end of round |

### Lifecycle × memory behavior

| Lifecycle | Personal memory | Team memory | Use case |
|-----------|-----------------|-------------|----------|
| **Temporary** | Read-only access to the parent agent's workspace memory | None | One-shot tasks, disposable teams |
| **Persistent** | Each member reads/writes its own | Auto-extracted, accumulates across rounds | Long-running collaboration where lessons should persist |

### Layout

```
~/.openjiuwen/.agent_teams/{team_name}/
├── team-memory/                          # Shared team memory
│   └── TEAM_MEMORY.md
├── workspaces/
│   ├── alice_workspace/                   # New member (created inside the team)
│   │   ├── memory/                        # personal memory (general scenario)
│   │   └── coding_memory/                 # personal memory (coding scenario)
│   └── bob_workspace -> ~/.openjiuwen/bob_workspace/   # Predefined member (symlink)
└── team-workspace/                        # Shared file area (not memory)
```

- **New members**: workspace freshly created under the team home; personal memory starts empty
- **Predefined members**: a symlink points to the existing personal workspace, so prior memory carries over
- Whether personal memory uses `memory/` or `coding_memory/` is decided at team creation by the scenario (`general` / `coding`)

### Auto-extracted team memory

In persistent teams, the **Leader** spawns an extractor agent at the end of every round. It reads the round's task records and team messages, distills what's worth keeping, and updates `TEAM_MEMORY.md`. Entries are tagged with one of four categories:

| Tag | Meaning |
|-----|---------|
| `[decision]` | Team decisions — why A was chosen over B, key trade-offs |
| `[lesson]` | Lessons learned — what worked, what caused rework, reusable patterns |
| `[member]` | Member strengths — who is good at what, who owns which area |
| `[context]` | Project context — business constraints, deadlines, stakeholder asks |

The extractor agent autonomously reads existing memory, analyzes the round, and merges / updates / evicts entries. The file is kept under 200 lines. Temporary teams do not extract.

### Isolation

- **Cross-team**: each team's `team_name` is part of the storage path and index key (`agent_id = "{team_name}.{member_name}"`); same-named members in different teams cannot see each other's memory
- **Cross-member**: each member has its own index instance; members cannot read each other's personal memory, only the shared team memory
- **Temporary teams**: read-only access to the parent workspace; the source memory cannot be polluted


## Retrieval

Memory retrieval uses the configured external provider. For GaussPD, tool names, parameter schemas and execution come directly from the registered MCP server. See [Celia prompt and GaussPD integration](../zh/Celia新版MCP工具适配.md) for mounting and validation details.
