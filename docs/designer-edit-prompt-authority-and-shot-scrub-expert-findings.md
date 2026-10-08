# Designer failure cases user-edit-prompt authority & stale-shot-reference scrub — expert findings

**Branch analyzed:** `0.2.8.beta1` @ `d164ad4`  
**Repo:** `jiuwenswarm`  
**Date:** 2026-10-06  
**Scope:** Analysis only — **no code changes** in this pass.

**Agents:**
- [Expert A — user-edit-prompt authority cup edit](11e5163a-67e0-4cfc-9ad9-42615f9b26d1)
- [Expert B — stale-shot-reference scrub shot delete](65f5c38d-b537-458b-aa57-c3913504e4b0)
- [Expert C — pipeline auditor](19f958ff-7489-4e84-b573-4174f855249a)

---

## Executive summary

| Case | User report | Root cause (consensus) | Confidence |
|------|-------------|------------------------|------------|
| **User-edit-prompt authority** | Second shot edited white cup → blue cup; UI/config showed blue; video request still used white | **Dual-authority prompt:** toolbar saves blue into `config.prompt` / `generate.prompt` / packet, but clip regenerate rebuilds the API prompt from unchanged **`shot_action` / `camera`** via leaf agent + `director_approve_video_prompt` | **0.90** |
| **Stale-shot-reference scrub** | After deleting third shot, edit second shot to close-up; error `Document n_brief references missing shots: [3, 5]`; edit not applied | **Stale brief after canvas delete** still references shot 3; chat document validation fail-closes. **`[5]` is a regex false positive** on `每个镜头 5 秒` | **0.88** |

**Shared architecture theme (Expert C):** User-visible surface edits (toolbar prompt, chat intent) are **not** the generation/sync source of truth. Beat fields and documents are; the system prefers **rewrite (user-edit-prompt authority)** or **reject (stale-shot-reference scrub)** over merging surface text into those authorities.

---

## Failure cases (as reported)

### User-edit-prompt authority
Change second shot from white cup to blue cup. Before execution, the blue cup prompt was saved, but the actual video request and generated `generate.prompt` still used the white cup; the UI page and `config.prompt` still displayed the blue cup.

### Stale-shot-reference scrub
After deleting the third shot, edit only the second shot to a close-up. Document validation reported `Document n_brief references missing shots: [3, 5]`, and the second shot edit was not completed.

---

# Expert A — user-edit-prompt authority (white cup → blue cup)

## Root cause hypothesis (ranked)

| Rank | Hypothesis | Type | Confidence |
|------|------------|------|------------|
| **1** | Execution does not treat toolbar `generate.prompt` / `config.prompt` as video authority. Clip path rebuilds API prompt from `shot_action` (+ `cast_actions` / scene beat) via leaf agent + `director_approve_video_prompt` → `compose_practice_prompt`. Semantic fields stay “white cup”. | Dual-field / multi-authority | **High** |
| **2** | Leaf agent rewrites from storyboard beat; may call `call_video_model` with white narrative even when scaffold shows blue. | Agent behavior | High |
| **3** | Director rewrite gate discards blue text when practice checks fail, re-seeding from white `shot_action` / camera. | Dual-field + lock rewrite | High |
| **4** | Prop/scene freeze / R2V refs reinforce white cup visually. | Continuity / prop lock | Medium (amplifier) |
| **5** | Save race (`persistBeforeRun` swallows flush errors). | Sync / persistence | Lower for nominal user-edit-prompt authority |

**Not primary for user-edit-prompt authority:** `chat_document_sync` (toolbar regenerate path, not chat/doc edit).

## Why UI showed blue but video used white

```
Toolbar edit (blue)
  → writeMediaGeneratePatch
      generate.prompt, config.prompt,
      last_wan_prompt, last_approved_prompt,
      regenerate_packet.prompt  = BLUE
  → flushSave (persistBeforeRun)

Clip regenerate (NodeAgent + call_video_model)
  → Authority for API body:
      shot_action / cast_actions / camera  = still WHITE
      + agent rewrite + director_approve_video_prompt
  → generate_clip_video(prompt=WHITE)

UI still shows BLUE from edited config / regenerate_packet
```

## Key files / functions

| Location | Role |
|----------|------|
| `frontend/.../mediaNodeConfig.ts` — `writeMediaGeneratePatch` | Writes blue into prompt surfaces; does **not** update `shot_action` / `camera` |
| `frontend/.../DesignerNodeToolbar.tsx` | Toolbar edit → rerun |
| `frontend/.../designerRunStore.ts` — `persistBeforeRun` | Save-then-run |
| `handlers/common.py` — `graph_prompt` | Clip prefers `generate.prompt` for agent *scaffold* |
| `node_agent.py` — `call_video_model` | Agent text / `build_clip_prompt` / fallbacks, then **always** lock/director path |
| `handlers/clip.py` — `build_clip_prompt` | Action from storyboard / `shot_action` |
| `pipeline/wan_call_locks.py` — `apply_wan_call_locks` | Passes `action=cfg.shot_action`, `camera=cfg.camera` |
| `pipeline/video_prompt_practice.py` — `director_approve_video_prompt` / `compose_practice_prompt` | May fully rewrite prompt from beat fields |

## Fields that stay white after toolbar-only edit

| Field | Updated by toolbar? | Used at video time? |
|-------|---------------------|---------------------|
| `config.prompt` / `generate.prompt` | Yes (blue) | Scaffold / UI — not final API authority |
| `regenerate_packet.prompt` / `last_*` | Yes (blue) pre-run | UI precedence; post-run may stamp sent (white) |
| **`shot_action`** | **No** | **Yes — primary beat** |
| **`camera`** | **No** | **Yes — camera line in compose** |
| Storyboard markdown / analysis | **No** | Yes via shot sync |

## Severity
**P0** — user-visible “edit prompt + regenerate” does not control the video API. Blast radius: all agent-delegated clip regenerates after prompt-only edits.

---

# Expert B — stale-shot-reference scrub (delete shot 3, edit shot 2)

## Root cause hypothesis (ranked)

1. **Primary:** Canvas delete of `n_clip_3` does not rewrite brief/storyboard. Later chat edit forces document sync + hard validation. Stale brief still contains `镜头 3` while graph shots are `{1,2}` → missing shot `3`.
2. **Primary companion:** `_SHOT_REFERENCE` false-positive on duration prose `每个镜头 5 秒` → synthetic missing shot `5`.
3. **Amplifier:** `[[REMOVED_SHOT:...]]` mapping only covers indices present in the **before** graph of the *current* chat turn. Prior-turn canvas delete means shot `3` is already gone → `镜头 3` is left unmarked.
4. **Secondary:** Document-editor LLM may fail to scrub stale refs; not required to explain `[3,5]` (unchanged brief already yields that set).

## Why validation said `[3, 5]`

- **`3`:** Real leftover refs to deleted third shot (`### 镜头 3｜…`, continuity tables, etc.).
- **`5`:** False match from **`每个镜头 5 秒`**. Regex `镜头\s*#?\s*([1-9]\d*)` treats “每个**镜头 5** 秒” as shot 5.

## Why the shot-2 edit aborted

Order in `run_leader_chat`:

1. Leader plan (edit `n_clip_2` toward close-up; `edit_documents` likely true).
2. `apply_leader_plan` → in-memory candidate graph.
3. Document sync LLM (`plan_document_edits`).
4. `prepare_document_update` applies text edits, then checks  
   `referenced_shot_indices(texts[key]) - {graph shot indices}`  
   for brief/storyboard.
5. On raise → `DesignerGraphValidationError` → **no save**. Shot-2 close-up never persisted.

## Key files / functions

| Area | Location |
|------|----------|
| Validation throw | `chat_document_sync.py` — `prepare_document_update` (~404–417) |
| Shot-ref regex | `chat_shot_references.py` — `_SHOT_REFERENCE`, `referenced_shot_indices` |
| Chat orchestration | `leader_chat.py` — `run_leader_chat` / `apply_leader_plan` |
| Error surface | `designer_adapter.py` — `_chat_graph` |
| Canvas delete (no doc rewrite) | `designerStore.ts` — `removeNodes`; `designerCanvasNodes.ts` — `removeNodesFromGraph` |
| Shot index identity | `designer_graph.py` — `node_shot_index` |

## Confirming artifacts

| Artifact | Shows |
|----------|--------|
| `JiuwenswarmDesign-test/A-20261001/cases/M14-stale-shot-reference scrub/` | After delete: clips 1–2 only; brief/storyboard still 3-shot; stale-shot-reference scrub error `[3, 5]` |
| `semantic-M14-reopen-shot-scrub-After.json` | Edit not persisted |
| N09/N10 (`B-P1-20261001`) | Same validator, `[5]` only, no delete — isolates duration false positive |

## Severity
**P0 / high.** Blocks chat content edits after canvas shot delete whenever brief/storyboard still name the removed shot; separately can block ordinary edits via `每个镜头 N 秒` false positive. Fail-closed before save (no partial corrupt graph), but requested edit never lands.

---

# Expert C — Pipeline auditor (cross-check)

## Verdict on A and B

| Expert | Stance | Confidence |
|--------|--------|------------|
| **A (user-edit-prompt authority)** | **Agree** with corrections | **0.90** |
| **B (stale-shot-reference scrub)** | **Agree** with minor nits | **0.88** |

Root causes **do not conflict**.

## Corrections to Expert A
1. Cup color authority is mainly **`shot_action` + `camera`** (+ storyboard row), not primarily `scene_specs`.
2. UI read path is **packet-first**: `regenerate_packet.prompt` → `last_wan_prompt` → `last_approved_prompt` → `generate.prompt` → root `prompt` — so UI can stay blue while `generate.prompt` / API go white.
3. Decisive choke point is **`apply_wan_call_locks` → `director_approve_video_prompt`**, not `build_clip_prompt` alone.

## Corrections to Expert B
1. Missing-shot raise is specifically ~414–417; 404–413 also cover continuity-redirect guards.
2. “K06” is a case/label, not an in-repo code symbol.
3. Even if LLM removes all `镜头 3` lines, **`每个镜头 5 秒` alone** can keep validation failing with `[5]`.

## Pipeline synthesis

```
Canvas / toolbar / chat UI
        │
        ▼
 Mutable surfaces (generate.prompt, packet, chat plan)
        │
        ▼
 Authoritative beat / docs (shot_action, camera, storyboard, n_brief)
        │
        ├─ clip run  → director rewrite → video API   (user-edit-prompt authority)
        └─ chat edit → prepare_document_update fail-closed (stale-shot-reference scrub)
```

| Theme | user-edit-prompt authority | stale-shot-reference scrub |
|-------|-----|-----|
| Dual authority | Toolbar prompt vs `shot_action`/`camera`/director | Chat intent vs persisted brief/storyboard |
| Delete vs doc sync | N/A | Topology delete without prose remap |
| Fail-closed / overwrite | Director overwrites API prompt toward beats | Validation blocks save on unresolved refs |
| Brittle text contracts | Natural-language cup color in multiple fields | Regex treats any `镜头 N` as shot id |
| UI honesty | Toolbar can show non-API prompt | Chat reports error; edit not applied |

## Interactions
- Same café demo corpus language (`每个镜头 5 秒`, white-cup brief) appears in both fixture families.
- Fixing only toolbar prompt (user-edit-prompt authority) would not fix stale-shot-reference scrub; fixing only delete+sync (stale-shot-reference scrub) would not make blue-cup toolbar edits stick on regenerate.
- Shared hardening theme: **shot-number and prop-color semantics need structured fields**, not prose regex / free-text overlays.

---

# Verification steps only (no implementation)

### User-edit-prompt authority
1. Diff `n_clip_2` across prompt-saved → after → reopen: `prompt`, `generate.prompt`, `regenerate_packet.prompt`, `last_wan_prompt`, `shot_action`, `camera`.
2. Confirm media-request prompt equals post-`apply_wan_call_locks` text (white camera/action line).
3. Control: change only `camera`/`shot_action` to blue → expect API blue.
4. Control: toolbar blue, beat fields white → expect API white (current bug).
5. Capture `director_approve_video_prompt` reasons (`director_rewrote*` vs kept agent prompt).
6. Confirm toolbar display uses `resolveMediaPromptForToolbar` (packet-first).

### Stale-shot-reference scrub
1. After canvas delete of shot 3 (before chat): list `referenced_shot_indices(brief)` vs live shot indices.
2. Unit-check regex on `每个镜头 5 秒`, `镜头 3`, BGM lines with `镜头1→镜头2→镜头3`.
3. Isolate FP: strip only `镜头 3` sections; re-run chat edit — if still fails on `[5]`, FP is sufficient.
4. Trace `leader_chat` → `plan_document_edits` → `prepare_document_update` at raise time.
5. Confirm `removeNodesFromGraph` never mutates brief/storyboard bodies.

### Cross
1. Same graph: delete shot 3 → toolbar-edit cup → chat-edit close-up — map which gate fires first.
2. Acceptance targets (future): API prompt ≡ toolbar-resolved prompt when `prompt_origin=user`; post-delete brief refs ⊆ live shot indices with duration phrases excluded.

---

# Bottom line

- **User-edit-prompt authority** is a **split-brain prompt authority** bug: UI/toolbar blue vs execution beat/director white.
- **Stale-shot-reference scrub** is **stale document prose after canvas delete** plus a **shot-number regex false positive** (`每个镜头 5 秒` → shot 5), fail-closing the chat edit before save.
- Expert C confirms both; no conflict; shared theme is surface edit ≠ authoritative beat/doc state.
