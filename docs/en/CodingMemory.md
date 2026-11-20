# Coding Memory

Single-agent Code mode supports native file memory or agent-memory document memory. One Coding memory slot selects the implementation; do not add its rails or tools to the dynamic configuration lists.

## Configuration

Edit the active `config.yaml`; the template is `jiuwenswarm/resources/config.yaml`. `modes.code.memory.enabled` controls Coding memory and `ProjectMemoryRail`, which reads local project instructions. It defaults to `true`. The repository template defaults `memory.builtin.provider` to `jiuwen` for document memory. An explicitly empty value, or a missing field in an older configuration, selects the native `CodingMemoryRail`.

The switches above apply to single-agent mode. For `team.work` and `team.code`, selecting `memory.builtin.provider=jiuwen` enables Team memory regardless of `modes.team.*.memory.enabled`, single-agent Work/Code switches or `memory.mode/engine`. Valid SDK/HTTP and model configuration is still required. An empty provider and `team.design` retain their existing behavior.

To select Jiuwen SDK document memory for single-agent Code:

```yaml
modes:
  code:
    memory:
      enabled: true
memory:
  mode: local
  engine: builtin
  builtin:
    provider: jiuwen
    jiuwen:
      mode: sdk
      user_id: my-user
      scope_id: my-org
      agent_id: main_agent  # Shared memory identity for Work/Code/Team
      session_id: ""        # Not automatically bound to the chat session
      space: ""
      sdk:
        markdown_store:
          default:
            target: local
            params:
              root: /absolute/path/to/memory  # Optional; otherwise use the shared default
```

SDK creates the Runtime and document watchdog inside Swarm. The repository template uses local `hashing` vectors (64 dimensions), so no embedding service is required. An LLM is still required; credentials not explicitly overridden are injected from the default model in Swarm `models.defaults`, without a separate agent-memory environment file. To use an embedding model, set `jiuwen.sdk.embedder.default.target` to `openai` and set `jiuwen.sdk.globals.embedder_dim` to the model dimension. Embedding connection settings not explicitly overridden are injected from Swarm `embed.*`. `jiuwen.sdk.globals` can override model settings, credentials and dimensions; see the template for all fields.

For HTTP, replace the `jiuwen` section above with:

```yaml
jiuwen:
  mode: http
  base_url: http://127.0.0.1:8137
  api_key: ""              # Match the server's authentication configuration
  identity_org: local
  identity_user: developer
  user_id: my-user
  scope_id: my-org
  agent_id: main_agent
  session_id: ""
  space: ""
```

HTTP requires a running agent-memory document service. The service owns the Runtime, models, storage and watchdog. Swarm creates no local memory Runtime, ignores `jiuwen.sdk`, and does not upload local memory files. Keep `memory.mode=local`: this selects Swarm's built-in route and is separate from the HTTP connection setting.

Both connections require compatible core and JiuwenMemory dependencies containing Coding support. Merging source alone does not update installed packages. The template defaults to `sdk`; when both the setting and `JIUWEN_MEMORY_MODE` environment variable are absent, the resolver falls back to `http`.

## Tools and Call Flow

| Route | Tool | Main arguments and purpose |
|---|---|---|
| Native | `coding_memory_read` | `path`, optional `offset/limit`; read a topic file |
| Native | `coding_memory_write` | `path/content`; write a topic file |
| Native | `coding_memory_edit` | `path/old_text/new_text`; edit a topic file |
| Jiuwen | `agent_memory_coding_memory_read` | `md_filename`, optional `offset/limit`; read `USER.md`, `MEMORY.md` or a dated file |
| Jiuwen | `agent_memory_coding_memory_write` | `content`; extract and classify durable facts, without a topic file path |
| Jiuwen | `agent_memory_coding_memory_edit` | `md_filename/old_text/new_text`; replace a unique match after reading |
| Jiuwen | `agent_memory_coding_memory_search` | `query`, optional `top_k` (default 5, range 1–50); search only the current project |

Normal Jiuwen turns read the user profile and retrieve project memories. Empty results or search failures fall back to reading the project's `MEMORY.md`. The main Agent and `code_agent` share the selected Rail/Provider. When both the user message and assistant reply are nonempty, the completed turn queues `sync_turn`; heartbeat and cron tasks do not trigger this extraction. An error or an empty extraction does not confirm a saved memory.

## Identity and Storage

The Jiuwen route uses `memory.builtin.jiuwen.agent_id` for Work, Code and Team, defaulting to `main_agent`. Runtime Agent names do not override it. With SDK, matching org/user/space and configured identity permit shared profiles, including assistants with different runtime names. Project facts stay project-scoped; team conclusions stay team-scoped. Different users or configured identities remain filtered by Scope. `session_id` is passed through and defaults to an empty string. Document reads match session, while existing shadow-index searches span sessions. HTTP identity mapping follows the service protocol and must match its authentication configuration.

The default `main_agent` retains the previous default memory identity. An explicit `agent_id` still takes precedence; change it to `main_agent` if access to that legacy identity is required. Existing data is not migrated or merged automatically.

Work and Code use the same project key: the canonical path's directory name plus a 12-character digest. Same-named projects at different paths remain separate; no project means `default`. Both modes can share project memories when connected to the same memory store, permitted by the identity filters, and using the same project path. Code tools cannot override this key. Work tools default to the current project and retain explicit coordinates for accessing existing memories.

| Route | Location |
|---|---|
| Native and AutoMemory | `{agent_workspace}/coding_memory/{project_key}/`: topic Markdown and the `MEMORY.md` index |
| Jiuwen profile | `{root}/USER.md` |
| Jiuwen project | `{root}/memory/{project_key}/MEMORY.md`, `daily_memory/YYYY-MM-DD.md` |
| Jiuwen shadow index | `{root}/memory/.shadow/shadow.db` |

SDK `{root}` defaults to `agent/workspace` under the user data directory, shared by Work and Code; an explicit `markdown_store.default.params.root` wins. Sharing a directory does not remove identity filters. Legacy topic files are not migrated automatically. With HTTP, the document paths belong to the memory service.

## AutoMemory and Failures

AutoMemory retains its original switches and legacy directory, including when Jiuwen is selected. Canonical `agent.code.*` requests, including the web UI, use global `auto_memory_enabled`; legacy `code.*` requests use `modes.code.memory.auto_coding_memory`. Both default to `false`; the memory master switch and existing trigger conditions still apply.

The AutoMemory extraction child uses legacy `coding_memory_*` tools and does not inherit the Jiuwen Coding Rail or tools. This can reduce model prefix-cache reuse. Jiuwen `sync_turn` is independent of AutoMemory's switch. Enabling both performs separate extractions and writes to separate directories, potentially adding model calls; their results are not merged automatically.

A Jiuwen Coding Rail creation or rebuild failure leaves the Coding slot empty. Reload unregisters and closes the previous instance without falling back to the native Rail. `ProjectMemoryRail` independently reads local project instructions. Correct the configuration and rebuild/reload to recover. Runtime request timeouts and service-side extraction errors retain their existing handling. Native Coding failure behavior is unchanged.

## Dreaming and Related Documentation

Dreaming retains its existing behavior; this integration does not redirect it to the new document directory. See [Memory](Memory.md#dreaming-sleep-time-memory-consolidation).

- [Modes](Modes.md)
- [Task Memory](TaskMemory.md)
- [Documentation home](../README.md)

With Jiuwen Team selected, the entry agent does not load project instructions. Coding responders may retain `ProjectMemoryRail`. Switching back to single-agent Code restores entry-level project instructions according to the existing memory switch.
