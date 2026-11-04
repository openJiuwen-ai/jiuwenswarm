# Application role sessions

Text-only NPCs can use a lightweight conversation profile. It retains a compact base system prompt, application character instructions, and the role's own dialogue, without developer-assistant prompts, project instructions, personal memory, skills, or tools.

Create a session through the existing Gateway Web `/ws` endpoint with `execution_profile: "app_role"` and non-empty `role_instructions` (up to 12000 characters). Include `create_token`, `persist_session: false`, `mode: "agent"`, a title, and the source session's `project_id` and `work_mode`.

## Team NPCs

Choose a Team at app creation when several characters should speak independently. Use `mode: "team"` with the same creation parameters and a fixed roster:

```json
"npc_team": {"members": [{"name": "fisherman", "title": "Fisherman", "role_instructions": "Speak as the old fisherman.", "desc": "Knows the sea"}]}
```

The primary NPC is the Team leader. Its instructions and display name come from `role_instructions` and `title`. The one to five supporting NPCs run in native member sessions with their own personas. The host assembles the Team on the first chat turn. Verify the response includes the profile, session ID, and a Team mode (`team.work.normal` or `team.code.normal`). Submit player text or serialized scene JSON only to that Team session, using its returned mode.

The leader assigns each teammate a speaking task for the turn. Each non-empty `chat.final` contains the producing character's own final text in `content`, with `member_name` and `display_name`. Match the session and turn (prefer `turn_request_id`) and render each utterance with its speaker's name. End the turn on `chat.processing_status` with `is_processing:false` and check that all expected characters spoke. The app UI displays final speech and essential status. The host retains internal context, reasoning, tool calls, and task events. Delete the Team session through the standard session deletion flow.

## Legacy lightweight consultations

Existing `mode:"agent"` requests with a roster retain independent role sessions and the `ask_<name>` consultation protocol. Use Team mode above for new apps with independent character speech. The roster is fixed at creation. Both modes reject host tool injection.

The application obtains its source session from `WORKSWARM_SOURCE_SESSION` and queries `session.get_metadata` for project ownership. Execution profile and project ownership are independent: NPCs remain visible under the project that created the application. Do not hardcode project paths or models.

The creation response must contain `execution_profile: "app_role"` and the actual session ID. If that marker is absent, report that the host needs upgrading rather than falling back to a general assistant. The profile and instructions are immutable; create a new session to change character rules.

Send only current player input or scene JSON through `chat.send`. Reuse one session per stable role. Wait for `chat.final`; acceptance is not an answer. The host restores recent complete dialogue pairs from that session's history. Model input is bounded to 48000 characters; oldest pairs are omitted without modifying persisted history. Oversized fixed instructions plus current input produce an explicit error. Existing default-model selection applies.

The base system prompt identifies the assistant as an application character, requires following character rules and the application output format, treats player and scene values as input data, and states its capability boundaries. The role has no host tools, project-file access, private memory, or access to other characters' conversations. In the lead-and-members form the lead prompt lists the member roster and consultation rules, while member prompts answer only the lead's consultations.

The legacy consultation form adds `npc.member` events (`phase: started | finished`, with member name, member session id, and an answer summary) alongside the existing `chat.delta`/`chat.final`; members never stream incremental text. Every consultation is persisted into the member session's own history.

Streaming, usage events, cancellation, forks, and deletion are supported. Forking the lead cascades fresh member sessions (member history is not copied); forking a single member detaches it into an independent NPC session. The lightweight consultation path does not support host tool equipment, pause/resume, cron execution, persistent memory, and explicit enhanced agent execution. Sessions without this profile retain existing behavior.

Deploy and restart the host before enabling the profile. Existing NPC sessions are not migrated automatically. Application restart may delete old NPC conversations if its existing lifecycle implements that behavior.

Before consulting, the host verifies that the member still belongs to the lead and is executable. A missing, deleted, or rebound member produces a consultation error without creating a general assistant. Gateway preserves the complete `npc.member` payload and includes the lead request ID.
