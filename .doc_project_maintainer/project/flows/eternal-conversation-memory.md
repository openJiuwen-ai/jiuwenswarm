---
id: eternal-conversation-memory
name: Eternal Conversation Memory
confidence: confirmed
last_updated: 2026-10-19
read_when: "Changing eternal conversation, context replacement, durable memory, or session adapter cleanup."
---

# Eternal Conversation Memory

## Flow

1. A new Web Session sends the draft choice as `persist_session` in its first `session.create`. AgentServer stores the boolean in Session metadata and returns it through create/list/restore. The value is immutable afterward; each chat turn derives the internal `eternal_conversation_enabled` adapter key from this authoritative metadata.
2. Work/Code Adapter configures the common Rail and disables overlapping semantic MemoryRail behavior. Code project/coding memory remains independent.
3. The Rail records the post-ContextProcessor model envelope, user/task events, tool calls/results, and final result into an append-only cursor/hash-chain Raw History. Foreground history is a byte-equivalent mirror. Large fields are content-addressed by canonical JSON digest, and a cursor/hash-linked `search.jsonl` plus task-local indexes provide bounded mechanical views without becoming semantic sources of truth.
4. After each successful Worker model call, the Rail freezes its exact model-visible System Prompt, message prefix, tool definitions, selected-model request configuration, parent Session lineage, and KV-cache affinity configuration. Seventy percent of the selected model's token context window is an extraction trigger and audit warning threshold, not a fork-validity limit: a completed natural-user-task boundary at or above that ratio still passes the exact Worker prefix to the Extractor. Fixed foreground byte thresholds do not participate in this decision. A failed foreground invocation is recorded as `task-failed`, never as an extractable `task-finished` boundary.
5. Background Agent 1 (Extractor) is a guarded OpenJiuwen DeepAgent fork of that exact Worker prefix. The fork adds exactly one new user message that stops foreground work, switches the active workspace to a private Extractor sandbox, and supplies the frozen evidence request plus run-specific staged-memory paths. Its model-facing tool schemas remain identical for prefix-cache reuse, while the Persist Session guard Rail rebinds inherited filesystem tools to that sandbox, limits inherited shell tools to read-only staged `dynamic-memory-cli` inspection, and denies unrelated Worker tools. Extractor alone performs semantic selection, Snapshot/UT maintenance, exact-name UT creation, and conflict resolution, and must write the same proposal it returns. Its run-scoped proposal `write_file` binding validates before writing and returns only concise corrective text to the same ReAct loop when a rule fails; it never echoes the proposal JSON. Harness then independently validates the actual prefix hash, provider cache-read token count, memory-cli usage, file/result equality, structure, evidence, and revisions before atomically publishing Pending UTs.
6. Background Agent 2 (Builder) uses the same `dynamic-memory-cli` Skill under its Builder-specific Agent Spec and contract. It reviews only whether the frozen Pending batch is structurally buildable, runs the deterministic staging helper, and cannot reject semantic omissions or rewrite meaning. Harness validates the staged result and performs canonical Pending-to-Built publication. Search returns both states. Extractor and Builder share Skill resources, not authority, workspaces, histories, or tools.
7. At a later foreground boundary, Harness prepares a projection in `BEFORE_INVOKE`, then replaces old working context in `ON_USER_MESSAGE` after ModelContext initialization and before the new query is admitted. Replacement requires published `covered_through == requested_cursor`; events added while extraction runs remain in the next uncovered range. Existing ContextProcessor behavior runs afterward, and the Rail records its final model-visible result.

## Ownership and Recovery

- Coordinator identity is `(feature root, Session id)` and outlives Adapter cleanup.
- Fork acceptance and Extractor calls are audited separately. Every frozen Worker prefix must match byte-for-byte. Provider cache-token counts remain diagnostic because routed endpoints can report partial hits; the appended Extractor instruction is new input.
- Extractor workspaces are resolved dynamically per Session and their identity participates in the private SysOperation id, so an existing framework resource cannot silently retain an older sandbox. Every extraction receives a unique staged-memory run directory; canonical Session memory remains Harness-owned and is never exposed as the Extractor's writable root.
- Automatic retrieval queries are derived only from the direct user-authored text; the Worker-visible prompt is not rewritten. Published UT priority is authoritative for result ordering, and unresolved constraint conflicts must remain visible in Snapshot until direct user evidence authorizes an override.
- `persist_session` participates in the `create_token` idempotency signature but not the prewarm `WarmKey`. Prewarm creates no metadata and performs no model turn, so the claimed Agent can enable the Rail from the real Session value without duplicating warm slots.
- Raw History is authoritative; foreground mirror and cursor state can only recover by replaying byte-equivalent durable records. Normal restart performs a bounded validation of the canonical, foreground, and search-view tail cursor/hash plus the canonical event hash. Any missing or divergent tail falls back to the full hash-chain scan and byte-equivalent mirror/search repair. This keeps large Sessions from blocking the runtime heartbeat while preserving fail-closed crash recovery.
- Any cursor/hash/session divergence fails closed.
- On a later Session turn after restart, persisted requested cursor and Pending rows reschedule Extractor/Builder work.
- A DeepAgent Extractor never falls back to the legacy response-JSON protocol when the exact Worker fork is unavailable. Harness keeps the requested cursor unpublished, records an `extractor-deferred` audit event, and lets the next successful foreground boundary supply an exact fork that covers the complete outstanding Raw History range.
- Large foreground/background inputs are content-addressed JSON blobs; JSONL retains path/hash/bytes metadata so exact model input is reconstructable without duplicate growth. Search/task views carry canonical cursor/hash backlinks and are recoverable from Raw History.
- An exhausted Extractor Agent run is retried once immediately against the same frozen cursor range, staged memory, and fork context; the retry is audited and cannot publish partial output. Only if that same-batch retry also fails is the terminal error persisted for recovery at the next natural-task boundary. Structural retry prompts carry the exact prior invalid JSON and precise field/index/length failure so semantic compression remains an Extractor decision.
- Proposal feedback is scoped to the current Extractor invocation and path. The model sees only validation text, can correct the proposal inside its ReAct loop, and cannot reuse the validator later.
- A structural Builder rejection is audited and promoted to the same durable fail-fast state; Pending remains published and unchanged until a later Builder retry.
- Final proof generation replays canonical cursor/previous-hash/event-hash validation and checks every search/task derived-view backlink before matrix acceptance.
- The 200-task acceptance runner restores a failed task's workspace checkpoint before retrying, then prefixes the retried `chat.send` with an audited recovery notice naming every invalidated request id. This prevents the still-live Agent Session from treating rolled-back replies, tool results, or inferred workspace state as current evidence; resume reconstructs the same invalidation list from `interrupted-tasks.jsonl`. The first attempt remains byte-identical to the natural user task.

## Evidence

See [eternal-conversation-memory-verification.md](./eternal-conversation-memory-verification.md).
