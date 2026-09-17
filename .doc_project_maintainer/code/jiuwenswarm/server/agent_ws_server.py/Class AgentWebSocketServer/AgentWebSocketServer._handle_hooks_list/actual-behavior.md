---
symbol: AgentWebSocketServer._handle_hooks_list
detail: actual-behavior
source: jiuwenswarm/server/agent_ws_server.py
---

# `AgentWebSocketServer._handle_hooks_list`

## Actual Role

Implements an AgentServer copy of the global `hooks.list` read contract. It ignores request params, synchronously reloads config.yaml, parses the hook model, builds per-event summaries that include counts plus complete matcher hook definitions, labels the source as config.yaml, and sends one locked response. Config/load/summary exceptions become a raw-error response; wire encoding and send occur outside the local try and therefore fall to the outer request boundary. Current Web and TUI surfaces also implement the operation locally rather than relying on this method.

## Key Signals

- Input/routing: No request fields are consumed. `_handle_message` can dispatch `HOOKS_LIST`, but current Web and TUI surfaces resolve independent local implementations.
- Output: One response with `events`, `disable_all_hooks`, and source=`config.yaml`, or a raw exception string. Full commands/prompts are included without authorization, redaction, schema version, or response-size limit; request metadata is not copied.
- Error/runtime: Config parsing and payload construction are synchronous and protected by the local try; encoding/send are outside it and can escape to generic outer handling.
- Main risk: Three copies can drift, while malformed or very large global hook definitions are presented as a successful, potentially sensitive list payload.
- Related evidence: Summary helper semantics are unit-tested; RPC transport, malformed entries, routing ownership, payload bounds, and surface parity are not. Tests were not run for this documentation-only re-audit.

## Detail Index

- Detail docs pending.
