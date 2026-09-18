---
id: eternal-conversation-memory
name: Eternal Conversation Memory
confidence: confirmed
last_updated: 2026-09-17
read_when: "Changing eternal conversation, context replacement, durable memory, or session adapter cleanup."
---

# Eternal Conversation Memory

## Flow

1. A new Web Session sends the draft choice as `persist_session` in its first `session.create`. AgentServer stores the boolean in Session metadata and returns it through create/list/restore. The value is immutable afterward; each chat turn derives the internal `eternal_conversation_enabled` adapter key from this authoritative metadata.
2. Work/Code Adapter configures the common Rail and disables overlapping semantic MemoryRail behavior. Code project/coding memory remains independent.
3. The Rail records the post-ContextProcessor model envelope, user/task events, tool calls/results, and final result into an append-only cursor/hash-chain Raw History. Foreground history is a byte-equivalent mirror. Large fields are content-addressed by canonical JSON digest, and a cursor/hash-linked `search.jsonl` plus task-local indexes provide bounded mechanical views without becoming semantic sources of truth.
4. After each successful Worker model call, the Rail freezes its exact model-visible System Prompt, message prefix, tool definitions, selected-model request configuration, parent Session lineage, and KV-cache affinity configuration. At a completed natural-user-task boundary, the Session coordinator accepts this Extractor fork only when the Worker input is no more than 70% of that model's token context window; fixed foreground byte thresholds do not participate in this decision.
5. Background Agent 1 (Extractor) is a guarded OpenJiuwen DeepAgent fork of that exact Worker prefix. The fork adds exactly one new user message containing the Extractor contract and frozen evidence request. Its model-facing tool schemas remain identical for prefix-cache reuse, while a fork guard rejects tool execution. Extractor alone performs semantic selection, Snapshot/UT maintenance, exact-name UT creation, and conflict resolution. Harness validates the actual prefix hash and provider cache-read token count, then validates structure/evidence/revisions and atomically publishes Pending UTs through the vendored dynamic-memory-cli.
6. Background Agent 2 (Builder) reviews only whether the frozen Pending batch is structurally buildable; it cannot reject semantic omissions or rewrite meaning. Harness then performs deterministic Pending-to-Built construction. Search returns both states.
7. At a later foreground boundary, Harness prepares a projection in `BEFORE_INVOKE`, then replaces old working context in `ON_USER_MESSAGE` after ModelContext initialization and before the new query is admitted. Replacement requires published `covered_through == requested_cursor`; events added while extraction runs remain in the next uncovered range. Existing ContextProcessor behavior runs afterward, and the Rail records its final model-visible result.

## Ownership and Recovery

- Coordinator identity is `(feature root, Session id)` and outlives Web/TUI Adapter cleanup.
- Fork acceptance and each Extractor model call are audited separately. Formal correctness requires every frozen Worker prefix to match byte-for-byte. Provider `cache_read_tokens`/`cache_tokens` remain a diagnostic because a routed endpoint may report a partial hit even for the exact prefix; the appended Extractor user instruction is new input and is never counted as inherited-prefix cache.
- Automatic retrieval queries are derived only from the direct user-authored text; the Worker-visible prompt is not rewritten. Published UT priority is authoritative for result ordering, and unresolved constraint conflicts must remain visible in Snapshot until direct user evidence authorizes an override.
- `persist_session` participates in the `create_token` idempotency signature but not the prewarm `WarmKey`. Prewarm creates no metadata and performs no model turn, so the claimed Agent can enable the Rail from the real Session value without duplicating warm slots.
- Raw History is authoritative; foreground mirror and cursor state can only recover by replaying byte-equivalent durable records.
- Any cursor/hash/session divergence fails closed.
- On a later Session turn after restart, persisted requested cursor and Pending rows reschedule Extractor/Builder work.
- Large foreground/background inputs are content-addressed JSON blobs; JSONL retains path/hash/bytes metadata so exact model input is reconstructable without duplicate growth. Search/task views carry canonical cursor/hash backlinks and are recoverable from Raw History.
- Extractor/Builder terminal errors are durable Session state. The next natural-task boundary clears and retries them; acceptance barriers fail fast when a worker has exited unsuccessfully. Structural retry prompts carry the exact prior invalid JSON and precise field/index/length failure so semantic compression remains an Extractor decision.
- A structural Builder rejection is audited and promoted to the same durable fail-fast state; Pending remains published and unchanged until a later Builder retry.
- Final proof generation replays canonical cursor/previous-hash/event-hash validation and checks every search/task derived-view backlink before matrix acceptance.

## Evidence

- Unit/regression: `tests/unit_tests/agentserver/rails/test_eternal_conversation_rail.py` and `test_eternal_conversation_deep_agents.py` cover lifecycle-safe replacement, exact Worker-prefix forking, the 70% token-window boundary, child Session lineage, one appended Extractor instruction, structural prefix/cache evidence, retries, batching, blob reconstruction, Builder responsibility, and worker-error recovery.
- Real model: `scripts/acceptance/eternal_conversation_200.py` drives Web/TUI × Work/Code with an explicitly selected configured model, records alias/actual model/endpoint, and emits per-quadrant proof. The current user-selected model is `Deepseek-V4-Flash-0731`.
- Accepted Web Code proof (2026-09-17): 200/200 tasks, 5/5 hidden probes, 49 context replacements, 12829 verified Raw History records, 511/511 byte-identical Extractor prefixes, 184 active Built UTs, zero Pending UTs, and final pytest return code 0.
- Normative design: `docs/zh/永续会话Rail实现规范.md`.
