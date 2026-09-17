---
id: agentserver-command-mcp
name: AgentServer MCP Command And Runtime Reload
status: partial
confidence: confirmed
last_updated: 2026-07-14
user_visible_surface: "TUI /mcp command, MCP server/tool browser, and MCP tools available to agents."
source_of_truth:
  - "config.yaml:mcp.servers"
modules:
  - gateway-and-channels
  - agentserver-runtime
  - agent-harness
directories:
  - jiuwenswarm/channels/tui/frontend/src
  - jiuwenswarm/gateway
  - jiuwenswarm/server
  - jiuwenswarm/common
code_symbols:
  - createMcpCommand
  - AgentWebSocketServer._handle_command_mcp
  - AgentManager.reload_agents_config
  - JiuWenSwarmDeepAdapter._sync_mcp_servers_for_runtime
entrypoints:
  - jiuwenswarm/channels/tui/frontend/src/core/commands/builtins/mcp.ts
  - jiuwenswarm/server/agent_ws_server.py
  - jiuwenswarm/common/config.py
---

# AgentServer MCP Command And Runtime Reload

## Outcome

TUI users manage MCP server definitions and browse their tools. Mutations first change `config.yaml:mcp.servers`, then attempt runtime reconciliation. Registered tools are shown as `mcp__{serverName}__{toolName}`.

## Causal Path

1. `createMcpCommand` and interactive MCP screens issue unary `command.mcp` requests. Slash actions are `list`, `show`, `add`, `update`, `enable`, `disable`, and `remove`; interactive screens also use `list_tools`.
2. TUI routing marks `command.mcp` for forwarding with no local handler. Gateway preserves request/session/params/metadata/agent identity and sends an E2A unary request through `MessageHandler` and `WebSocketAgentServerClient`.
3. The client correlates the response by `request_id`, serializes socket sends, and enforces its unary timeout. AgentServer decodes `ReqMethod.COMMAND_MCP`, calls `_handle_command_mcp`, and sends one `AgentResponse` under the connection send lock.
4. Reads load and mask configuration. `show`/`list_tools` prefer tools in the process-global Runner resource manager and may create a temporary MCP client when no registered tools are found for an enabled server.
5. Mutations normalize fields, perform a YAML read-modify-write, then call `reload_agents_config(get_config(), None)`. No target scope means all active channel agents are considered.
6. Each deep adapter reconciles enabled entries against tracked registrations by server name: remove missing, add new, and remove/re-add changed servers. Registration adds the server and ability to Runner; HTTP-family servers receive reachability preflight.
7. The correlated response returns to TUI transcript or server/tool selectors and detail panels.

## State Classification

- **Durable source of truth:** `config.yaml:mcp.servers`, operationally keyed by trimmed `name`; entries contain enabled/transport state and stdio or network connection fields.
- **Transient applied state:** Runner server ids, abilities and tool cards, live clients/subprocesses, and adapter registration maps.
- **Transport state:** websocket connections, Gateway pending queues, locks, and E2A envelopes are request-scoped.
- **Derived output:** masked lists/details, tool counts/schemas, selectors, and transcript rows are reconstructed from configuration or discovery.
- **Observability:** logs and preflight messages describe attempts; they do not prove desired and applied state agree.

## Replay, Restore, Or Reconstruction

- New/restarted agents read effective configuration and register enabled entries, allowing later convergence after hot-reload failure.
- Disk and runtime changes are not transactional. YAML is written before reload; a reload exception is not rolled back and returns `ok: true, applied: false`. Later reload or restart is the recovery path.
- Whole-agent reload updates the root adapter; cached session adapters are marked stale and can reconstruct lazily on next use.
- `list`/`show` reconstruct from YAML. Tools come from the active registry or a temporary connection; there is no durable tool inventory.

## Contract

- **Method:** unary `command.mcp` / `ReqMethod.COMMAND_MCP`; `request_id` correlates the reply. Carried `session_id` does not scope MCP configuration changes.
- **Actions:** `list`; `show` with optional name; `add`; partial `update`; `enable`; `disable`; `remove`/`delete`; `list_tools`. Normalization accepts `stdio`, `sse`, `http`, `streamable-http`, and `streamable_http`, though one error message lists only the first three.
- **Mutation results:** types include `added`, `updated`, `enabled`, `disabled`, and `removed`, with `name`, `applied`, and optional masked item/error. Failed local-file precheck is `ok: false, type: add_failed`; reload failure is `ok: true, applied: false`.
- **Read results:** `list` returns masked items; `show` returns masked detail and optional tool count; `list_tools` returns descriptors/schema. Discovery failures soften to an empty list or zero count.
- **Errors:** missing names map to `MCP_NOT_FOUND`, validation to `MCP_BAD_REQUEST`, and other exceptions to `MCP_INTERNAL`; error text is the exception string.
- **Disclosure:** recursive masking uses a limited heuristic and cannot guarantee arbitrary environment/header names are safe. Slash detail also omits `env`/`cwd`.

## Consumer State And Output

Slash output contains list/detail blocks and mutation messages; failed requests become `mcp failed: ...`. Interactive views use `list`, `show`, mutations, and `list_tools` for selectors and details. They treat `ok: true` as success even if the payload has `applied: false`: payload detail may expose the error, while the headline still says added/removed/enabled/disabled. Agents consume eventual Runner registrations, not the command response.

## Failure, Ordering, And Identity

- YAML write failure returns a failed response before reload. Reload failure happens after durable mutation and creates a desired/applied split.
- A successful `reload_agents_config` return does not prove application: adapter registration can log reachability/registration failure and return `false` without raising, while the handler reports `applied: true`.
- Add preflight only covers enabled `stdio` entries whose arguments look like local files. Temporary discovery has no handler-level timeout and collapses failures to empty output.
- Gateway serializes sends and AgentManager serializes reload bodies, but AgentServer handles frames in separate tasks. YAML read-modify-write is outside the reload lock, with no observed revision/file lock; concurrent mutations may reorder or lose updates. The response lock orders writes, not effects.
- `request_id` identifies the exchange; server `name` identifies configuration/reconciliation. Add is upsert, repeated enable/disable may skip reload, absent remove fails, and no separate idempotency token exists.
- `show`/`list_tools` consult a process-global registry by server name. Isolation for same-named servers across agents/channels is not established and remains inferred.

## Verification

Static inspection confirmed TUI producers, Gateway forwarding, E2A correlation, AgentServer branches, YAML helpers, reload scope/lock, and adapter reconciliation. `test_agentserver_cli_commands.py` covers masked list, add reload, missing enable, remove, update, and an in-memory flow. `test_agent_reload_scope.py` covers reload scoping with MCP sync mocked. Tests were not executed.

## Known Gaps

- No located test drives TUI -> Gateway -> AgentServer -> real YAML -> Runner registration or asserts `applied: false` presentation.
- No located test covers `show`, `list_tools`, masking edge cases, post-write reload failure, restart convergence, temporary-client timeout, or concurrent mutations.
- Registration failures do not propagate into `applied`; the API has no per-agent convergence report.
- Same-name ownership in the global Runner lookup needs an explicit isolation contract/test.
- Command-side temporary-client conversion and runtime `common.mcp_config` conversion can drift; transport aliases and validation wording already differ.
