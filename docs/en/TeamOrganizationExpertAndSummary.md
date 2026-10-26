# Team Organization Expert and Summary Team Configuration

JiuwenSwarm scans local, built-in, and resource `agent_groups` roots. A package is discoverable only when it has a valid `manifest.json`, `package_type` is `agent_group`, the manifest name matches the safe directory name, no other source contains the same name, and the AgentGroup bundle passes validation.

Scanning returns descriptors only. `org_create_and_invite_expert_team` combines the selected package's roles, prompts, skills, and capabilities with the current Team's model, storage, transport, workspace, and session defaults. Each instance receives a unique runtime Team ID; read it from `org_view_organization` rather than treating the package name as an ID.

Direct messages to a running expert Team use the same `interact` channel as ordinary Teams. Acceptance means the input was delivered to the Leader runtime, not that its task has completed. Replies continue through the existing Team stream and are saved in that Team's conversation history. A paused expert still resumes through the existing execution path. Organization-internal task and Leader-message scheduling is unchanged.

## Summary Team

Choose aggregation before decomposing a claimed root. Select aggregation from root requirements and actual Team delivery scopes. When no specialist Team is responsible for the complete root deliverable and Teams provide parallel independent domain conclusions that must be collected, organized or synthesized, you must choose SUMMARY_TEAM. Do not switch to HIERARCHICAL because the output is short, the task is small, conflicts are absent, time or overhead can be reduced, collection is simple, or the Owner can compile the results. Choose HIERARCHICAL only when one specialist Team owns the complete root delivery and its final output itself satisfies the root; other Teams supply inputs, components or validation. Domain orthogonality does not change this condition. Nominal Owner aggregation is not complete specialist delivery; do not invent a final integrator. Honor an explicit user choice within runtime constraints.

The fixed Summary Team contains:

| Member | Responsibility |
|--------|----------------|
| `summary-leader` | Validates sources, coordinates drafting, and submits the result |
| `source-integrator` | Creates a source-attributed outline from the read-only snapshot |
| `delivery-drafter` | Produces the user-facing deliverable |

It uses the configured default model but does not copy user Team members or prompts. Intermediate work stays in its Team workspace; final artifacts belong under the Organization `summary/` directory. One persistent Summary Team is lazily created per Organization and reused.

Summary inputs include `root_task` (ID, title, description and output specification) alongside the existing bound sources. Root and Summary Task requirements determine format and acceptance; factual conclusions still come only from bound sources. Preserve disagreements, limitations and accepted artifact locations instead of always producing another report. To publish an existing artifact as the final file, copy it unchanged into `summary/`; `result_uri` must still point there, with the actual `result_type`.

For this local development checkout, `uv sync --locked --inexact` installs the sibling `../../agent-core-0.1.20/agent-core_0.1.20-zeze` as an editable dependency. Python changes in that checkout are used after restarting the backend; `uv run` no longer replaces it with the Git-pinned package.

For production, use a database reachable by all Teams, provide genuinely shared artifact storage, keep model defaults resolvable, and use stable capability labels. Do not prelaunch every expert Team in configuration; let the Owner Leader create them when the root task requires them.

On binding failure, the launcher rolls an expert Team back. Summary Team lifecycle and Summary Execution state are persisted separately so restart recovery can restore the shared Team and re-evaluate source readiness.

`WAITING_SOURCES` means aggregation has not started. Required sources must be both `COMPLETED` and `ACCEPTED`, including contributions executed by the Owner Team itself. The creation tool returns the current task status and `blocked_sources` while waiting; the Owner should address unfinished work or pending reviews before waiting for delivery. Accepted sources continue to trigger the existing activation path.

For an organization child assigned to its own Team, the Leader should call `org_update_task(action='start')` before execution or internal dispatch. When teammates finish, the Leader checks their results and promptly completes the corresponding organization child; its creator Team still reviews it explicitly. Internal task completion does not automatically complete or accept the organization child.

Chat messages, timeline headers and tool groups resolve the Leader identity within the selected Team's conversation. Without a saved identity, they use that Team's roster or display name. Runtime Teams such as Summary do not inherit the Owner's name or avatar, and this display resolution does not change AgentGroup bindings.

Task completion and Web execution status have separate lifecycles. When a background Organization turn ends, the Web client closes that Team's current reasoning and streaming output without ending other Teams' work. A passive `attach_only` subscription does not keep the session busy; real execution and requests waiting for user input still do. Check Summary Execution and task states to confirm completion, rather than treating an intermediate `chat.final` segment as the end of the turn.

## Importing packages

Imports validate skills before saving or installing the package. String entries in `skills` must be skill names, such as `"docx-pro"`; mappings use package-relative directories, such as `{"dir": "skills/docx-pro", "mode": "all"}`. Absolute POSIX and Windows paths are rejected, including Windows paths uploaded to a non-Windows server. Referenced skill directories must remain inside the package and contain `SKILL.md`.
