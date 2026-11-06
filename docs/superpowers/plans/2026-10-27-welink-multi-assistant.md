# WeLink Multi-assistant Implementation Plan

> **For agentic workers:** Use test-driven implementation with separately owned files; integrate and review all boundaries before delivery.

**Goal:** One employee can keep multiple WeLink colleagues online, with per-colleague routing, readable first catch-up and confirmed local handoff.

**Architecture:** A shared reader owns CLI login, membership and cursors. Assistant channels own credentials, sockets and delivery journals. Shared archive is per group; Requests and access are per colleague. WeLink reconciliation remains isolated from the general delivery loop.

**Tech Stack:** Existing Python asyncio gateway/server, SQLite repositories and React colleague settings.

**Spec:** User-supplied `welink-multi-assistant.txt` (provided with the development request), sections 1–10. Implement steps 1–2; native platform mentions await verified protocol.

## Global Constraints

- Personal CLI is read-only. No create-group/add-member/send fallback.
- One reader account. Unique colleague IDs and assistant credentials; secret-free public status.
- Unknown delivery is never automatically resent. Reconcile latest history page only.
- Keep Feishu behavior and colleague-level passive/adaptive setting.
- Existing active access boundaries never move because another colleague joins.

## Review Focus

- Legacy single binding and unresolved account-key delivery migration.
- Duplicate credentials and account mismatch must fail before connection mutation.
- Shared archive conflict must not suppress another colleague's independent Request.
- Slow/failed member and delivery checks must not cause cross-channel blocking or resends.
- First catch-up, concurrent mention/history and repeated handoff must preserve deduplication and visibility.

## Tasks

- [x] Configuration/UI: migrate old section to bindings; update only named colleague; shared reader login; per-colleague public statuses, revoke and validation. Tests for two bindings, duplicates, migration and secret masking.
- [x] Inbound/access: plural binding lookup; validate credential and colleague; shared archive with idempotent per-colleague acceptance; passive/adaptive; original archive times and scoped first catch-up. Tests for duplicates, second colleague, access and retries.
- [x] Reader/lifecycle: one reader and stable per-AK channel keys; update only changed channels; reader remains alive on binding-only changes; group identity mapping must be verified, fail closed if unknown. Tests for lifecycle, targeting, retry and first catch-up.
- [x] Confirmed handoff: persist resolved targets, send visible names, invoke only after confirmed history receipt/current membership, idempotent per target/history ID. Keep Feishu code path unchanged.
- [x] Run targeted regression and inspect full diff, document limits and update/create feature PR on jiuwenmate_0.2.8.

## Verification outcome

Affected gateway and JiuwenMate unit directories: 2280 passed, 1 skipped, 1 deselected, with the baseline-failing bash test file excluded. Five bash failures and the deselected unpublished-knowledge-page test were reproduced on unmodified ab036e12. Full-suite run was interrupted after 67 passed and 320 seconds without completing; it is not a full-suite pass. Frontend interaction tests: 2 passed; Vite production build passed. No live WeLink end-to-end run or deployment.
