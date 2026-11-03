# Application role sessions

Text-only NPCs can use a lightweight conversation profile. It retains a compact base system prompt, application character instructions, and the role's own dialogue, without developer-assistant prompts, project instructions, personal memory, skills, or tools.

Create a session through the existing Gateway Web `/ws` endpoint with `execution_profile: "app_role"` and non-empty `role_instructions` (up to 12000 characters). Include `create_token`, `persist_session: false`, `mode: "agent"`, a title, and the source session's `project_id` and `work_mode`.

The application obtains its source session from `WORKSWARM_SOURCE_SESSION` and queries `session.get_metadata` for project ownership. Execution profile and project ownership are independent: NPCs remain visible under the project that created the application. Do not hardcode project paths or models.

The creation response must contain `execution_profile: "app_role"` and the actual session ID. If that marker is absent, report that the host needs upgrading rather than falling back to a general assistant. The profile and instructions are immutable; create a new session to change character rules.

Send only current player input or scene JSON through `chat.send`. Reuse one session per stable role. Wait for `chat.final`; acceptance is not an answer. The host restores recent complete dialogue pairs from that session's history. Model input is bounded to 48000 characters; oldest pairs are omitted without modifying persisted history. Oversized fixed instructions plus current input produce an explicit error. Existing default-model selection applies.

The base system prompt identifies the assistant as an application character, requires following character rules and the application output format, treats player and scene values as input data, and states its capability boundaries. The role has no tools, project-file access, private memory, or access to other characters' conversations.

Streaming, usage events, cancellation, forks, and deletion are supported. Tools, pause/resume, teams, cron execution, persistent memory, and explicit enhanced agent execution are unsupported. Sessions without this profile retain existing behavior.

Deploy and restart the host before enabling the profile. Existing NPC sessions are not migrated automatically. Application restart may delete old NPC conversations if its existing lifecycle implements that behavior.
