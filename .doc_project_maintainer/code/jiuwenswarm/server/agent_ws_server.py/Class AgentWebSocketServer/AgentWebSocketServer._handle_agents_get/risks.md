---
symbol: AgentWebSocketServer._handle_agents_get
detail: risks
source: jiuwenswarm/server/agent_ws_server.py
---

# AgentWebSocketServer._handle_agents_get audit evidence

## ISSUE-001: An unvalidated workspace selector can disclose complete agent definitions.

- Dimension: `boundary_safety`
- Severity: `high`
- Status: `open`
- Evidence: Current handler passes params.workspace_dir directly to AgentConfigService and serializes the resulting full dataclass. The service accepts any Path, reads .jiuwenswarm/agents and agents-local below it, and the response includes prompt plus absolute file_path without authorization or redaction.
- Suggested action: Resolve an authenticated project identity, constrain it to authorized roots, and omit host paths unless required.

## ISSUE-002: Builtin enabled state can remain stale across reads.

- Dimension: `implementation_soundness`
- Severity: `medium`
- Status: `open`
- Evidence: AgentConfigService.get_agent calls list_agents, whose sources use list(BUILTIN_AGENTS), a shallow list of global objects. It assigns enabled only for names present in the current config, so removing an entry does not reset a previously mutated builtin before serialization.
- Suggested action: Clone builtin definitions for every resolution or explicitly reset derived fields before applying the current config snapshot.

## ISSUE-003: A single-name lookup synchronously parses every agent file on the event loop.

- Dimension: `performance_risk`
- Severity: `medium`
- Status: `open`
- Evidence: Before the first await, service.get_agent invokes list_agents, scans local/user/project directories, reads/parses every Markdown body and global config, then linearly searches the full merged list; definition count and prompt size are uncapped.
- Suggested action: Move discovery off-loop and use a cached, precedence-aware lookup for the normalized name.

## ISSUE-004: Handler and boundary contracts lack direct tests.

- Dimension: `test_coverage`
- Severity: `high`
- Status: `open`
- Evidence: test_agent_config_service.py covers service get/list behavior, but repository search finds no agents.get, AGENTS_GET, or _handle_agents_get test for workspace authorization, disclosure, stale enabled state, malformed/large files, errors, or wire output.
- Suggested action: Cover valid/not-found/missing names, source precedence, workspace authorization, stale enabled state, malformed/large files, error redaction, and Web/TUI wire contracts.

## ISSUE-005: Missing and malformed agent names are treated as ordinary not-found lookups.

- Dimension: `input_contract`
- Severity: `medium`
- Status: `open`
- Evidence: The handler defaults name to an empty string and does no type/format normalization; get_agent compares it directly, then the response returns ok=false with interpolated input but no stable BAD_REQUEST or NOT_FOUND code.
- Suggested action: Require a normalized string name and distinguish validation failure from a stable not-found result.
