# Team Organization Expert and Summary Team Configuration

JiuwenSwarm scans local, built-in, and resource `agent_groups` roots. A package is discoverable only when it has a valid `manifest.json`, `package_type` is `agent_group`, the manifest name matches the safe directory name, no other source contains the same name, and the AgentGroup bundle passes validation.

Scanning returns descriptors only. `org_create_and_invite_expert_team` combines the selected package's roles, prompts, skills, and capabilities with the current Team's model, storage, transport, workspace, and session defaults. Each instance receives a unique runtime Team ID; read it from `org_view_organization` rather than treating the package name as an ID.

Direct messages to a running expert Team use the same `interact` channel as ordinary Teams. Acceptance means the input was delivered to the Leader runtime, not that its task has completed. Replies continue through the existing Team stream and are saved in that Team's conversation history. A paused expert still resumes through the existing execution path. Organization-internal task and Leader-message scheduling is unchanged.

## Summary Team

The fixed Summary Team contains:

| Member | Responsibility |
|--------|----------------|
| `summary-leader` | Validates sources, coordinates drafting, and submits the result |
| `source-integrator` | Creates a source-attributed outline from the read-only snapshot |
| `delivery-drafter` | Produces the user-facing deliverable |

It uses the configured default model but does not copy user Team members or prompts. Intermediate work stays in its Team workspace; final artifacts belong under the Organization `summary/` directory. One persistent Summary Team is lazily created per Organization and reused.

For production, use a database reachable by all Teams, provide genuinely shared artifact storage, keep model defaults resolvable, and use stable capability labels. Do not prelaunch every expert Team in configuration; let the Owner Leader create them when the root task requires them.

On binding failure, the launcher rolls an expert Team back. Summary Team lifecycle and Summary Execution state are persisted separately so restart recovery can restore the shared Team and re-evaluate source readiness.

Task completion and Web execution status have separate lifecycles. When a background Organization turn ends, the Web client closes that Team's current reasoning and streaming output without ending other Teams' work. A passive `attach_only` subscription does not keep the session busy; real execution and requests waiting for user input still do. Check Summary Execution and task states to confirm completion, rather than treating an intermediate `chat.final` segment as the end of the turn.

## Importing packages

Imports validate skills before saving or installing the package. String entries in `skills` must be skill names, such as `"docx-pro"`; mappings use package-relative directories, such as `{"dir": "skills/docx-pro", "mode": "all"}`. Absolute POSIX and Windows paths are rejected, including Windows paths uploaded to a non-Windows server. Referenced skill directories must remain inside the package and contain `SKILL.md`.
