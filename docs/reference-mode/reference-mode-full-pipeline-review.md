# Expert Audit: `reference-mode-full-pipeline.md` vs Live Code

**Auditor role:** technical auditor (read-only; no production code changes)  
**Branch audited:** `0.2.8.beta1-referenceModeFix` (confirmed at `jiuwenswarm/`)  
**Spec under review:** `reference-mode-full-pipeline.md`  
**Live modules:**

| Module | Actual path (git root = `jiuwenswarm/`) |
|---|---|
| Topology / plan | `jiuwenswarm/server/runtime/designer/pipeline/reference_led.py` |
| Uploads / classify | `jiuwenswarm/server/runtime/designer/user_references.py` |
| Enter bootstrap | `jiuwenswarm/server/runtime/gateway_adapter/designer_adapter.py` |
| Graph router | `jiuwenswarm/server/runtime/designer/smart_graph.py` |
| Scenario tests | `tests/unit_tests/test_reference_led_scenarios.py` |

---

## Post-review follow-ups (parent agent)

After this audit, the pipeline MD was patched for `needs_set` plates, handler→agent promotion, classify-empty fail, fail-closed scope, attachment limits, and `_SCENARIO_KINDS`. `reference-mode-test-catalog.md` was added. Combined pytest: **10029** scenario/intent tests + **26** user_references = green. A full second audit pass was not re-run; original verdict below stands for the pre-patch MD.

---

## Verdict: **Partial**

The MD captures the intended Enter → classify → stamp → fail-closed → `build_reference_led_video_graph` story, the verbatim-default bindings, companion-cast coverage, and the main `_job_plan` forks well enough for a high-level walkthrough.

It is **not** reproduce-ready for a new engineer: several claims are overstated or incomplete relative to code, mode-graph diagrams omit real edges, Enter’s post-build Director / `apply_runtime_delegate` path is missing, and the test scenario matrix is not listed.

---

## Section-by-section fidelity

| MD section | Claim (summary) | Code evidence | Fidelity |
|---|---|---|---|
| Header / modules | Paths under `jiuwenswarm/server/...`; repo root `jiuwenswarm/` | Nested package lives at `jiuwenswarm/jiuwenswarm/server/...`; relative to git root the table is OK. Workspace root `openjiuwen-ai/` needs one more `jiuwenswarm/` segment. | Match (with path caveat) |
| Intro bullets | use-as-is defaults; base64 materialize-before-classify; companions under authority style | `_DEFAULT_BINDING` all `verbatim` (`reference_led.py:56-65`); `materialize_user_references_for_analysis` (`user_references.py:856-866`); `_style_lock_from_references` + `_companion_sheet_prompt` (`reference_led.py:1034-1067`, `986-995`) | Match |
| §1 Still verbatim → handler card | `n_ref_*`, pixels never image-gen’d | Builder: `force_handler`, `immutable_source`, optional `reference_card_role` (`reference_led.py:488-525`, `_verbatim_card_role` `1003-1021`) | Match |
| §1 Still condition → regen | `identity_sheet` / `medium_change` / scene plate | Condition char sheets (`531-564`); motion restyle (`597-625`); condition scene plates via `restyle_scene` (`1118-1162`) | Match |
| §1 Uncovered people/**places** → companions | “not covered by a still → companion sheets/**plates**” | Characters: always uncovered − covered unless suppress (`1092-1100`). **Places only if `needs_set`** = scene role OR `(product AND analysis.scenes AND not motion)` (`1125-1131`). Pure character + incidental `analysis.scenes` does **not** mint plates (`1125-1128`, tests `character_family` / `test_verbatim_character_multi_cast…`). | **Mismatch** (places overstated) |
| §1 Suppress / solo / keyframe | LLM flags → no companions | `_suppress_companions` intent+slots (`879-888`); stamps intent flags (`173-179`) | Match |
| §1 Multi-still coverage | Each still covers its id; only uncovered companions | `_covered_character_ids` / `_covered_setting_ids` (`908-968`); multi-still test (`1153-1186`) | Match |
| §1 No phrase regex | Bindings from LLM JSON only | Classify prompt forbids fixed phrases (`user_references.py:244-246`); `_carry_intent_fields` comment (`809-813`); forbidden-phrase test (`481-484`) | Match |
| §2 Enter mermaid: materialize → analyze → classify → stamp | Order of operations | `_bootstrap_graph_with_director_impl` `1347-1402` | Match |
| §2 Fail closed if images but not `reference_led` | After project normalize + rebase | `_bootstrap_graph` `916-928` | Match |
| §2 Never `dest_dir=None` for classify | Base64 would skip stamp | Docstring + materialize helper (`user_references.py:856-866`); preview path empty for base64 (`912-938`) | Match |
| §2 `rebase_creative_intent_paths` | Slots → `.designer/refs` | `_bootstrap_graph` `892-917`; `rebase_creative_intent_paths` `882-909` | Match |
| §2 Router → `build_reference_led_video_graph` | `creative_intent.mode == reference_led` | `smart_graph.py:861-872` via `reference_led_active` | Match |
| §2 Attach refs after graph | `attach_user_references_to_graph` | `_bootstrap_graph` `972-973` | Match |
| §2 Mermaid ends at Director author → Play | Implies author then Play | After sync bootstrap, Director authors brief/storyboard/graph + validate (`designer_adapter.py:1426-1467`). Mermaid collapses this and omits **classify empty → hard fail** (`1388-1392`) and second `apply_runtime_delegate` (`1451`). | Partial |
| §3 Roles / bindings / flags | Five roles; verbatim/condition; set_lock / style_authority / suppress trio | Constants `22-65`; flags carried `809-830` | Match |
| §4 `_job_plan` mermaid | suppress → cover → motion i2v vs r2v → needs_set plates | `_job_plan` `1069-1201` | Match (main forks) |
| §4 Coverage rules | Char/scene id normalize; motion+cid; motion+solo cast | `_covered_character_ids` `908-923`; motion-solo cover `1095-1099`; `_norm_id` `845-849` | Match |
| §4 Companions never beyond analysis | | `_companion_characters` / `_companion_scenes` `926-983` | Match |
| §5 Node table: `n_ref_*` force_handler | | `488-525` | Match |
| §5 `n_character_*` / `n_restyle_01` / `n_scene_*` **Delegate = agent** | “agent after stamp” / “agent” | Builder sets **`delegate: "handler"`** for sheets, companions, restyle, plates (`550`, `588`, `615`, `649`) **without** `force_handler`. Enter then runs `apply_runtime_delegate` (`designer_adapter.py:937-947`, `1451`), which upgrades non-`force_handler` nodes to **agent** (`smart_graph.py:87-121`). Unit tests calling `build_smart_video_graph` alone keep handlers. | Partial / misleading |
| §5 Clip = agent + compose_reference_clip_prompt + call_video_model | | Clip config `685-710`; prompt compose `275-327` | Match |
| §5.1–5.6 Mode graphs | Simplified LR flows | Real DAG also: every `n_ref_*` → `n_brief` (`526`); brief→storyboard; sheets/plates from storyboard; clip inputs from plan / I2V first-frame + sheet_ids (`691-721`). Diagrams omit brief and ref→brief. | Partial (topology simplified) |
| §5.1 Family companions | Upload card + companion sheets; no Xiaoyue regen | `character_family` assertions (`367-377`); intent test (`990-1021`) | Match |
| §5.2 Solo | No companions when only covered id | `test_verbatim_character_solo…` (`1024-1030`) | Match |
| §5.3 Condition + companions | Self sheet + companions | `character_condition_family` (`378-387`); `test_condition_character_sheets_self_plus_companions` (`1033-1046`) | Match |
| §5.4 Scene verbatim + cast + extra set | Companion sheets + plate for uncovered set | `scene_cast` (`357-366`); `test_scene_verbatim_builds_plates…` (`1049-1082`) | Match |
| §5.5 Product + cast | Companions + store plate if scenes | `product_cast` (`311-320`); `needs_set` product∧settings (`1128`) | Match |
| §5.6 Motion + multi-cast | I2V; companions under style_lock; suppress skips | `motion_cast` (`337-345`); motion branch `plates=False` (`1166-1183`); residual about non-R2V slots is accurate (`video_generation_overrides` i2v clears `reference_images` `193-201`) | Match |
| §6 `reference_image_plan` order + cap 5 | product → verbatim chars → sheets → scenes → plates; `_cap_plan` | `_reference_plan` `1204-1224`; `_cap_plan` `1227-1236` | Match |
| §7 Style inheritance | ensure_style_lock; authority/set_lock → vision medium or `match_reference_still` | `413-419`, `_style_lock_from_references` `1034-1067` | Match |
| §8 Reproduce checklist | branch, pytest path, manual smoke | Branch matches. Pytest from `jiuwenswarm/` is correct. Manual smoke aligns with family tests. Does **not** list the 10 `_SCENARIO_KINDS`. | Partial |
| §9 Files of record | Points at this review + `reference-mode-test-catalog.md` | **`reference-mode-test-catalog.md` does not exist** in the workspace | **Mismatch** |
| §10 Residuals | Wan generative; I2V companions not R2V slots; no invented set on pure character; cap 5 | Consistent with code | Match |

---

## Whether graphs match topology

**Decision graph (§4):** Matches `_job_plan` closely enough (suppress, coverage, motion i2v vs r2v, `needs_set`, no invented set for pure character).

**Mode graphs (§5.1–5.6):** Behaviorally right for node *kinds* and Wan call mode, but **not** full topology:

| Edge / node in code | In MD diagrams? |
|---|---|
| `n_ref_*` → `n_brief` (`reference_led.py:526`) | No |
| `n_brief` → `n_storyboard` | Mostly omitted |
| Condition sheet inputs: storyboard **and** upload (`549-564`) | Partially (5.3 shows upload→sheet only) |
| Companion sheet: storyboard only, `companion_cast=True`, `require_reference_images=False` (`566-595`) | Yes (intent) |
| I2V: first_frame path/node + **all** `sheet_ids` on clip inputs (`691-701`) | 5.6 shows companions→CLIP |
| R2V: plan node_ids wired as clip inputs (`702-706`) | Implied |
| `n_compose` after clips | Only in §5 table, not mode diagrams |
| `metadata.bootstrap = designer.graph.reference_led.v1` (`757`) | Checklist only |

**Router:** `build_smart_video_graph` reference_led branch (`smart_graph.py:861-872`) matches §2. Classic path is `smart_video.quality.v5` only when **not** reference-led (MD names that failure mode correctly).

---

## Companion / Enter / base64 coverage accuracy

### Companions — mostly accurate, one hard overclaim

| Topic | MD | Code | Accuracy |
|---|---|---|---|
| Uncovered cast → companion sheets | Yes | `_companion_characters` + builder loop `566-595` | Accurate |
| Style from film `style_lock` | Yes | `_companion_sheet_prompt` / copied `style_lock` | Accurate |
| Suppress trio | Yes | Intent + per-slot (`879-888`) | Accurate |
| Motion may still mint companions | Yes (5.6) | Motion returns companions; `plates=False` (`1166-1183`) | Accurate |
| Uncovered **places** always companion plates | §1 yes | Only when `needs_set` and not suppress (`1128-1131`) | **Inaccurate** |
| Condition scene still → generated plate | Implied in §4 | `restyle_scene` inserted into `plate_scenes` (`1132-1162`) | Under-documented but code OK |
| Product covers no cast ids → all analysis cast companions | Implied 5.5 | Confirmed `product_cast` test `311-314` | Accurate |

### Enter — order correct; several failure / post-steps missing

Documented correctly:

1. `require_llm` (`designer_adapter.py:1344-1346`)
2. `materialize_user_references_for_analysis` before classify (`1347-1354`)
3. `analyze_creative_brief` with image paths (`1365-1369`)
4. `classify_reference_images` → `stamp_creative_intent` (`1384-1402`)
5. Sync `_bootstrap_graph`: project `normalize_user_references` → `.designer/refs` (`892-898`)
6. `rebase_creative_intent_paths` (`917`)
7. Fail closed if image records and not `reference_led_active` (`924-928`)
8. `build_smart_video_graph` → attach refs (`937-973`)

Missing from MD but required to reproduce Enter behavior:

| Gap | Code |
|---|---|
| Classify returns `[]` → **fail immediately** (not silent classic graph) | `1388-1392` |
| `stamp_creative_intent` raises `ReferenceIntentError` if a still has no roles | `reference_led.py:154-157`; adapter `1401-1402` |
| Soft catch: materialize exception → `user_refs_preview = []` (can skip reference-led) | `1357-1358` |
| Director author/review brief → storyboard → `design_execution_graph` → validate | `1426-1450` |
| `apply_runtime_delegate` after build **and** after Director | `937-947`, `1451` |
| `director_composed_on_bootstrap` / `freeze_shot_topology` stamped | `954-957`, `1458` |
| Max **3** image refs (`MAX_REFS_BY_KIND`) | `user_references.py:50` |
| Inline base64 cap **6MB** | `51`, `1066-1070` |
| `analysis_prompt_with_references` wraps prompt with roster | `1359` |

### Base64 — accurate critical rule

| Claim | Status |
|---|---|
| Do not classify with `normalize(..., dest_dir=None)` for base64 | **Correct** — preview leaves `path=""` (`912-938`); classify skips empty path (`215-217`); `image_reference_records` requires path (`869-879`) |
| Use `materialize_user_references_for_analysis` first | **Correct** (`856-866`, Enter `1347-1354`) |
| Rebase after project copy | **Correct** (`882-909`, `_bootstrap_graph` `917`) |

---

## Missing details that would block reproduction

1. **Scenario kinds matrix** — live `_SCENARIO_KINDS` (`test_reference_led_scenarios.py:55-66`):  
   `product`, `motion`, `scene`, `character`, `text`, `character_family`, `character_condition_family`, `product_cast`, `scene_cast`, `motion_cast`  
   (10 × 1000 parametric cases). MD never lists them; points at a missing `reference-mode-test-catalog.md`.
2. **`needs_set` rule** for plates (scene role OR product+scenes; never pure character/motion inventing from incidental scenes) — only in residual §10.3, contradicted by §1.
3. **`apply_runtime_delegate`** — why sheet/plate nodes become agents on Enter despite builder `delegate=handler`.
4. **Classify empty-reads hard fail** vs fail-closed-after-rebase (two different failure points).
5. **Full edge list** for a family verbatim graph (ref→brief→storyboard→companions→clips→compose; plan order on clips).
6. **Attachment limits** (3 images / 1 video / 1 audio; 6MB inline).
7. **`style_source` role behavior** — only wired as restyle inputs when motion+condition (`602-603`); no dedicated mode graph.
8. **Product `reference_card_role`** — `_verbatim_card_role` returns `"product"` for any product role without checking binding (`1019-1020`); MD implies card roles are verbatim-only for “all five.”
9. **Motion-solo cover** when motion still has no `character_id` but analysis has one cast member (`1095-1099`) — §4 mentions motion+cid and motion+single cast partially; OK but easy to miss.
10. **Post-graph Director chain** — engineer following only §2 may think bootstrap ends at `attach_user_references_to_graph`.
11. **Referenced catalog file missing** — §9 lists `reference-mode-test-catalog.md` which is absent.

---

## Incorrect / outdated claims

1. **§1 places → companions always** — Incorrect vs `needs_set` (`reference_led.py:1125-1131`).
2. **§5 Delegate column “agent” for character/scene/restyle** — Incomplete/wrong if read against builder alone; true only after `apply_runtime_delegate` (`smart_graph.py:109-120`). Builder explicitly sets `handler` (`550`, `588`, `615`, `649`).
3. **§5 “agent after stamp”** — “Stamp” usually means `stamp_creative_intent`; delegate flip is **`apply_runtime_delegate`**, not stamp.
4. **§9 `reference-mode-test-catalog.md`** — File does not exist (outdated / aspirational).
5. **Mode diagrams as full topology** — Missing `n_brief` and `n_ref→n_brief`; risk of wrong edge expectations when debugging prune/wiring.
6. **§2 mermaid `E -->|no| H[text film analysis only]`** — Slightly loose: no image paths means classify/stamp skipped; analysis still ran with optional empty `reference_images`. Not a separate “text film” codepath until router sees no `reference_led` mode.

---

## Recommended MD edits (list only)

1. Fix §1: uncovered **characters** → companion sheets; uncovered **settings** → plates **only when `needs_set`** (scene still present, or product+`analysis.scenes`), never for pure character/motion from incidental scenes.
2. §5 node table: document builder `delegate=handler` for sheets/plates/restyle; Enter upgrades via `apply_runtime_delegate` unless `force_handler` (`n_ref_*` stay handlers).
3. Expand §2 with: classify `[]` fail; Director author/review chain; dual `apply_runtime_delegate`; attachment limits; soft materialize except.
4. Add § for `_SCENARIO_KINDS` + what each asserts (or create the missing catalog and link it).
5. Annotate mode mermaids: “clip dataflow / Wan contract” vs “full DAG,” and add brief + ref→brief.
6. Document `style_source` (restyle inputs only) and product card-role always-on quirk.
7. Correct §9: remove or mark `reference-mode-test-catalog.md` as TODO.
8. Clarify workspace vs git-root paths (`openjiuwen-ai/jiuwenswarm/jiuwenswarm/server/...` vs `jiuwenswarm/server/...`).
9. §4: call out motion-solo cover when `len(cast)==1` and no character still (`1095-1099`).
10. §6: note `_cap_plan` keeps first product + last scene, middle filled (`1227-1236`) — already sketched; add that truncated companions can disappear from Wan inputs.

*(No MD patches applied in this pass — gaps are documented here per task preference.)*

---

## Final score

| Question | Answer |
|---|---|
| Verdict | **Partial** |
| Graphs match topology? | **Decision graph: yes. Mode graphs: behavioral yes, structural partial.** |
| Companion / Enter / base64 accuracy? | **Enter order + base64: strong. Companions: strong for cast, weak/wrong for places. Enter post-steps incomplete.** |
| Reproduce-ready for a new engineer? | **No** |

A new engineer could pass the listed pytest file and roughly predict family/product/motion graphs from the MD, but would mis-predict set plates from §1, misread node delegates from §5 vs raw builder output, and miss Enter hard-fails and Director/`apply_runtime_delegate` behavior needed to reproduce live UI Enter end-to-end.
