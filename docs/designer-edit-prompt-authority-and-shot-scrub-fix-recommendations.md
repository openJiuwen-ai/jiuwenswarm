# Designer P0 fix recommendations — user-edit-prompt authority (cup prompt) & stale-shot-reference scrub (shot delete / chat edit)

**Branch:** `0.2.8.beta1-A-P0-Fixes` @ `d164ad4`  
**Repo:** `jiuwenswarm`  
**Date:** 2026-10-06  
**Scope:** Recommendations only — **no implementation in this pass**.

**Author:** [Expert D — fix recommendations](e76229e8-7112-41ab-8606-485411501dc8)

**Prior findings:** [`designer-edit-prompt-authority-and-shot-scrub-expert-findings.md`](./designer-edit-prompt-authority-and-shot-scrub-expert-findings.md) (Experts A / B / C)

---

## Prior findings (summary)

| Case | User report | Consensus root cause | Confidence |
|------|-------------|----------------------|------------|
| **User-edit-prompt authority** | Shot 2: white cup → blue cup; UI/`config.prompt` show blue; video API stays white | Dual-authority prompt: toolbar writes blue surfaces; regenerate rebuilds API prompt from unchanged `shot_action` / `camera` via director | **0.90** |
| **Stale-shot-reference scrub** | Delete shot 3; chat-edit shot 2 to close-up → `Document n_brief references missing shots: [3, 5]`; edit not saved | (1) Canvas delete leaves stale brief/`镜头 3`. (2) `_SHOT_REFERENCE` FP: `每个镜头 5 秒` → shot `5` | **0.88** |

**Shared theme:** User-visible surfaces are not the generation/sync source of truth. Beat fields and documents are; the system prefers rewrite (user-edit-prompt authority) or reject (stale-shot-reference scrub).

---

## Goals / non-goals

### Goals

1. **user-edit-prompt authority:** When `prompt_origin=user` (toolbar edit), the **video API body** must reflect that edit (blue cup), not stale white `shot_action` / `camera`.
2. **user-edit-prompt authority:** Preserve director locks for storyboard/agent runs (non-user origin).
3. **stale-shot-reference scrub:** Canvas shot delete must not permanently block later chat content edits via stale brief refs.
4. **stale-shot-reference scrub:** Duration idioms like `每个镜头 5 秒` must not be treated as shot indices.
5. **Honesty:** After success, UI / saved config / documents must not disagree with what the pipeline accepted.
6. **Fail-closed where it still matters:** No silent continuity redirects onto unrelated surviving shots; no corrupt partial saves.

### Non-goals (this P0)

- Full structured prop/color schema migration.
- Rewriting the entire director / practice-prompt system.
- Softening document validation to warn-only.
- Auto-renumbering all historical shot indices on every canvas op.
- Vendor / R2V / billing changes.
- Implementation in this document pass.

---

## Recommended fix — User-edit-prompt authority

### Choke points (confirmed)

```
Toolbar edit (blue)
  → writeMediaGeneratePatch (prompt_origin=user; does NOT update shot_action/camera)
  → persistBeforeRun

Clip regenerate
  → call_video_model → apply_wan_call_locks
        action=cfg.shot_action (WHITE), camera=cfg.camera (WHITE)
        → director_approve_video_prompt → compose_practice_prompt
  → generate_clip_video(prompt=WHITE)

UI still shows BLUE (packet-first resolveMediaPromptForToolbar)
```

**Decisive gate:** `apply_wan_call_locks` → `director_approve_video_prompt` (not `build_clip_prompt` alone).

### Preferred — edit-prompt-A: Honor `prompt_origin=user` in director / WAN lock gate

| `prompt_origin` | Behavior |
|-----------------|----------|
| `user` + practice-shaped prompt | **Keep** user text (`kept_user_prompt`); light trim only |
| `user` + needs rewrite (lock essay / missing Image-N) | Rewrite via compose, but seed **action/camera from user prompt**, not stale white beats |
| `user` + empty | Fall back to current beat path |
| absent / `storyboard` | **Unchanged** current director behavior |

**Same change set must also:**

1. Bypass beat-fidelity reasons that compare against stale white `shot_action` when origin is user.
2. Resolve user text with **generate.prompt first** when origin=user (then packet / last_* / root).
3. Post-run stamp honesty: stamp `generate.prompt` / `last_*` / packet with **actually sent** API text.
4. Apply on both `node_agent.call_video_model` and handler clip path that calls `apply_wan_call_locks`.

**Files:**

| File | Change |
|------|--------|
| `pipeline/wan_call_locks.py` | Resolve user-origin; pass user beat seeds |
| `pipeline/video_prompt_practice.py` | User-origin branch in director_approve / prepare / practice checks |
| `node_agent.py` | Ensure generate/origin reaches locks; stamp sent prompt |
| `handlers/clip.py` | Same origin-aware path if used for regenerate |
| `tests/unit_tests/designer/` | user-edit-prompt authority unit cases |

**Why preferred:** Smallest blast radius; preserves director for storyboard; matches existing `prompt_origin` semantics in `designer_graph.py`.

### Alternatives

| Option | Idea | Verdict |
|--------|------|---------|
| **edit-prompt-B** | Sync toolbar prompt into `shot_action`/`camera` on save | Phase 2 companion — brittle extraction |
| **edit-prompt-C** | Always restore `regenerate_packet` prompt | **Not sufficient alone** — still loses to director |
| **edit-prompt-D** | Structured prop/color fields | Phase 3 — too large for P0 |

### User-edit-prompt authority acceptance

1. Toolbar blue + regenerate → media request contains **blue**, not white.
2. Storyboard/agent origin still director-rewrites lock essays / image binding.
3. Control: only `shot_action`/`camera` blue (storyboard origin) → API blue.
4. After success, toolbar-resolved prompt and last sent prompt agree on cup color.
5. Non-user wardrobe/seat/exited-cast locks still enforced.

### User-edit-prompt authority risks

| Risk | Mitigation |
|------|------------|
| User pastes lock essay | Still rewrite for banners/FORBID/missing Image-N; seed action from user text |
| Leaf agent narrates white | User-origin director path must win **after** agent text |
| Over-broad bypass | Gate strictly on `prompt_origin == "user"` + non-empty prompt |

### User-edit-prompt authority tests

- Unit: user origin + blue generate.prompt + white shot_action → approved text blue.
- Unit: storyboard origin + blue generate.prompt + white beat → may rewrite toward white beat (intentional).
- Unit: user-origin lock essay → practice form, user-seeded action.
- Regression: existing director rewrite / compose coverage tests.
- Manual: café user-edit-prompt authority corpus.

---

## Recommended fix — Stale-shot-reference scrub

### Choke points (confirmed)

**A. Stale brief:** `removeNodesFromGraph` drops nodes; does **not** rewrite brief/storyboard. Later chat → `prepare_document_update` → `referenced_shot_indices - live shots` → raise; no save.

**B. Regex FP:** `_SHOT_REFERENCE` matches `每个镜头 5 秒` as shot **5**.

### Preferred — shot-scrub-A: Regex fix + missing-shot tombstone/scrub (both required)

#### Stale-shot-reference scrub-A1 — Regex false-positive (quick win)

**File:** `chat_shot_references.py`

1. Exclude matches when number is followed by duration units (`秒`, `s`, `sec`, `second(s)`, `分钟`, …).
2. Exclude when `镜头`/`shot`/`分镜` is preceded by `每个`/`每一`/`各`/`每` or `each`/`every`/`per`.
3. Keep true refs: `### 镜头 3｜…`, tables, `shot #3`, BGM chains `镜头1→镜头2→镜头3`.

Prefer filter-after-`finditer` with shared `_is_shot_index_reference`. Use in both `referenced_shot_indices` and `map_shot_references`.

#### Stale-shot-reference scrub-A2 — Stale missing-shot refs after prior canvas delete

1. **Tombstone in `document_edit_context`:** for each shot-ref whose index ∉ live indices, map to `[[REMOVED_SHOT:missing:{n}]]` even if not in current-turn `before` graph.
2. **Deterministic scrub in `prepare_document_update` before validate:** drop/neutralize missing-shot sections and inline refs; then re-check; only then raise.
3. Optional: include `user_canvas_edits` remove ops in planning context.

**Files:** `chat_shot_references.py`, `chat_document_plan.py`, `chat_document_sync.py` (`prepare_document_update`).

**Do not:** soften validation to warn-only (shot-scrub-C) or skip doc sync for clip-only edits (shot-scrub-D).

### Alternatives

| Option | Verdict |
|--------|---------|
| **shot-scrub-B** Canvas delete-time doc rewrite | Phase 2 after A proves chat path |
| **shot-scrub-C** Warn instead of throw | **Reject** — corrupt docs |
| **shot-scrub-D** Skip sync when topology frozen | **Reject** — leaves docs permanently stale |

### Stale-shot-reference scrub acceptance

1. `referenced_shot_indices("每个镜头 5 秒") == set()`.
2. Keepers: `### 镜头 3｜…` → `{3}`; `镜头1→镜头2→镜头3` → `{1,2,3}`.
3. Live shots `{1,2}` + stale brief with `镜头 3` + `每个镜头 5 秒` + chat close-up on shot 2 → **save succeeds**; no missing `[3,5]`; close-up persisted.
4. No silent continuity redirect 3→2 (`redirects_removed_continuity` remains).
5. N09/N10-style (no delete, duration only) must not fail on `[5]`.

### Stale-shot-reference scrub risks

| Risk | Mitigation |
|------|------------|
| Over-filtering real refs | Unit matrix: headings, arrows, EN/CN, duration |
| Scrub deletes too much | Only missing indices; section delete only when heading owns that shot |
| LLM leaves markers | Scrub markers before validate |

---

## Shared architecture recommendations

1. **Declare precedence** (user origin vs storyboard beat vs agent+director vs documents) — implement user-origin + live-index ⊆ prose rows in P0.
2. **Structured shot/prop fields (Phase 2+):** node id + index; prop color tokens — regex becomes compatibility only.
3. **Canvas topology vs docs:** update together **or** leave tombstone channel for next `prepare_document_update`.
4. **UI honesty:** do not “fix” user-edit-prompt authority by only changing the toolbar resolver.

---

## Implementation order

### Phase 0 — Characterization
Failing unit tests for user-edit-prompt authority + stale-shot-reference scrub acceptance (red).

### Phase 1 — P0 land order

| Order | Item | Case | Effort |
|------:|------|------|--------|
| 1 | **shot-scrub-A1** regex filter | stale-shot-reference scrub `[5]` | XS |
| 2 | **edit-prompt-A** user-origin director gate | user-edit-prompt authority | S–M |
| 3 | **shot-scrub-A2** tombstone + deterministic scrub | stale-shot-reference scrub `[3]` | S–M |

### Phase 2
Optional toolbar→`shot_action` sync (edit-prompt-B); delete-time doc scrub (shot-scrub-B); richer tombstones; `persistBeforeRun` error surfacing.

### Phase 3
Single `resolve_video_prompt_authority(...)`; structured document shot graph.

---

## Verification checklist

### User-edit-prompt authority
| # | Pass criteria |
|---|---------------|
| edit-prompt-1 | Toolbar blue → `generate.prompt` blue, `prompt_origin=user` |
| edit-prompt-2 | Regenerate → API prompt **blue**, not white |
| edit-prompt-3 | Post-run stamps agree on blue |
| edit-prompt-4 | Reopen: toolbar blue; no split-brain |
| edit-prompt-5 | Storyboard origin still director-rewrites lock essays |
| edit-prompt-6 | Only beat fields blue → API blue |
| edit-prompt-7 | Director notes: `kept_user_prompt` or rewrite without white-beat storyboard force |

### Stale-shot-reference scrub
| # | Pass criteria |
|---|---------------|
| shot-scrub-1 | Canvas delete → clips `{1,2}` only |
| shot-scrub-2 | `每个镜头 5 秒` not a shot ref |
| shot-scrub-3 | Chat close-up on shot 2 saves; no `[3, 5]` |
| shot-scrub-4 | Brief loses missing-3 refs; duration phrase OK |
| shot-scrub-5 | No silent 3→2 continuity redirect |
| shot-scrub-6 | Duration-only brief does not fail on `[5]` |
| shot-scrub-7 | Reopen shows close-up applied |

### Cross
| # | Pass criteria |
|---|---------------|
| X-1 | Delete 3 → toolbar blue → chat close-up: both pass |
| X-2 | Reference-led / continuity unit suites green |

---

## Out of scope / do not regress

**Do not regress:** director lock-essay rewrite for non-user origins; wardrobe/seat/exited-cast rules; `redirects_removed_continuity`; `prompt_origin=user` protection vs storyboard overwrite; reference-led director branch; `freeze_shot_topology`; fail-closed after scrub still finds true missing refs.

**Out of scope:** full prop schema; leaf-agent skill rewrites; ComfyUI; soft validation; billing/R2V policy.

**PR intent line:**  
> Director locks remain authoritative for storyboard/agent generations. User toolbar edits (`prompt_origin=user`) become authoritative for the video API body. Document validation remains fail-closed, but duration idioms are not shot ids, and missing shots after canvas delete are tombstoned/scrubbed before validate.

---

## Appendix — code anchors

| Concern | Path |
|---------|------|
| Toolbar write | `channels/web/frontend/.../mediaNodeConfig.ts` |
| Canvas delete | `designerCanvasNodes.ts` / `designerStore.ts` |
| Video lock gate | `pipeline/wan_call_locks.py` |
| Director | `pipeline/video_prompt_practice.py` |
| Agent call | `node_agent.py` — `call_video_model` |
| Packet restore | `pipeline/wan_prompt_hygiene.py` |
| Shot regex | `chat_shot_references.py` |
| Validate | `chat_document_sync.py` — `prepare_document_update` |
| Doc plan | `chat_document_plan.py` |
| Chat order | `leader_chat.py` |
| Findings | `designer-edit-prompt-authority-and-shot-scrub-expert-findings.md` |

---

## Bottom line

1. **user-edit-prompt authority:** Prefer **user-origin authority in the WAN/director gate** (edit-prompt-A). Do not disable director for storyboard. Packet restore alone is insufficient.
2. **stale-shot-reference scrub:** Land **regex FP fix** + **missing-shot tombstone/scrub** (shot-scrub-A1+A2). Do not soften validation to warn-only.
3. **Order:** tests red → regex → user-edit-prompt authority user-origin → stale-shot-reference scrub scrub → acceptance on café / M14–stale-shot-reference scrub fixtures → Phase 2 structured fields / delete-time doc sync.
