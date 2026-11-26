# Coding Memory

The bundled **Agent Memory** plugin provides memory for single-agent Work and Code sessions. It is enabled by default for new sessions and can be turned off in the session’s extensions panel. Turning it off preserves stored memories and stops recall and writeback. The legacy route does not take over.

## Configuration

In WorkSwarm personal edition, the memory plugin uses only the built-in HTTP service. WorkSwarm starts the authenticated listener on loopback during startup and stops it during shutdown. The plugin connects automatically; its detail page shows whether the service is running. SDK mode, custom service URLs and saved client API keys no longer apply. Enterprise does not start this single-identity listener.

`memory.plugin.default_enabled` controls the default for new sessions; existing sessions keep their saved choice. New sessions default to on regardless of legacy memory settings such as `memory.engine: none`. The listener starts independently of this setting. `modes.code.memory.enabled` still controls the separate `ProjectMemoryRail`. In `team.work` and `team.code`, `memory.builtin.provider=jiuwen` selects the plugin route and the session extension switch controls its use. `team.design` and an empty provider retain their original behavior.

The service still reads model settings from `memory.builtin.jiuwen.sdk`, with overrides under `plugins.agent-memory-plugin.sdk`. This historical key configures server-side models, not an SDK connection. The repository template uses local hashing for embeddings. Model-based extraction requires a real LLM; without explicitly configured credentials, the service uses the default Swarm model.

## Tools and Call Flow

| Route | Tool | Main arguments and purpose |
|---|---|---|
| Jiuwen | `agent_memory_coding_memory_read` | `md_filename`, optional `offset/limit`; read `USER.md`, `MEMORY.md` or a dated file |
| Jiuwen | `agent_memory_coding_memory_write` | `content`; extract and classify durable facts, without a topic file path |
| Jiuwen | `agent_memory_coding_memory_edit` | `md_filename/old_text/new_text`; replace a unique match after reading |
| Jiuwen | `agent_memory_coding_memory_search` | `query`, optional `top_k` (default 5, range 1–50); search only the current project |

Normal Jiuwen turns read the user profile and retrieve project memories. Empty results or search failures fall back to reading the project's `MEMORY.md`. The Code plugin mounts on the main Agent only. When both the user message and assistant reply are nonempty, the completed turn queues `sync_turn`; heartbeat and cron tasks do not trigger this extraction. An error or an empty extraction does not confirm a saved memory.

## Identity and Storage

The Jiuwen route uses `memory.builtin.jiuwen.agent_id` for Work, Code and Team, defaulting to `main_agent`. Runtime Agent names do not override it. Work and Code share the built-in library; project facts use project keys and team conclusions use team keys.

The default `main_agent` retains the previous default memory identity. An explicit `agent_id` still takes precedence; change it to `main_agent` if access to that legacy identity is required. Existing data is not migrated or merged automatically.

Work and Code use the same project key: the canonical path's directory name plus a 12-character digest. Same-named projects at different paths remain separate; no project means `default`. Both modes can share project memories when connected to the same memory store, permitted by the identity filters, and using the same project path. Code tools cannot override this key. Work tools default to the current project and retain explicit coordinates for accessing existing memories.

| Route | Location |
|---|---|
| Native and AutoMemory | `{agent_workspace}/coding_memory/{project_key}/`: topic Markdown and the `MEMORY.md` index |
| Jiuwen profile | `{root}/USER.md` |
| Jiuwen project | `{root}/memory/{project_key}/MEMORY.md`, `daily_memory/YYYY-MM-DD.md` |
| Jiuwen shadow index | `{root}/memory/.shadow/shadow.db` |

`{root}` is `agent/memory/managed-http` under the Swarm data directory. Existing data from the built-in service is reused. Old SDK and external HTTP libraries are not automatically migrated or deleted.

## Relationship to legacy memory

Work and Code main agents no longer start the legacy Rail, AutoMemory extraction, or Dreaming. This avoids duplicate writes. If the built-in HTTP listener is offline, the turn continues and the session keeps its memory selection. Goal reattachment does not switch mounted plugins; memory can be reattached on the next ordinary turn. Other plugin load errors are reported. Team members use the same plugin settings and the Team Rail. Team membership operations are skipped while the built-in service is offline rather than using an older store. Turning the plugin off disables Team recall and writeback without starting the native Team memory manager. Independently enabled MCP connectors remain available.

- [Modes](Modes.md)
- [Task Memory](TaskMemory.md)
- [Documentation home](../README.md)

With Jiuwen Team selected, the entry agent does not load project instructions. Coding responders may retain `ProjectMemoryRail`. Switching back to single-agent Code restores entry-level project instructions according to the existing memory switch.
