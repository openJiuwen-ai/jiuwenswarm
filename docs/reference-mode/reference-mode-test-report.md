# Reference-Led Pipeline — Test Report (40k)

**Branch:** `0.2.8.beta1-referenceModeFix`  
**Date:** 2026-10-05  
**Command:**

```bash
.venv/Scripts/python.exe -m pytest \
  tests/unit_tests/test_reference_led_scenarios.py \
  tests/unit_tests/designer/test_user_references.py \
  -q --no-cov
```

**Result:** **40066 passed** in ~3m17s (Windows / Python 3.13).

---

## What changed under test

| Area | Assertion focus |
|---|---|
| R1 `video_binding` | Default `multi_ref_story` → R2V; `animate_keyframe` solo → I2V; +cast → R2V |
| R2 classify | Prompt no longer steers dinner/act/advertise → I2V; stamp persists `video_binding` |
| R3 packing | Uploads + companions/plates always intended for model refs |
| R4 resolve | `planned` **merged** with edge fallback; companions not dropped when upload resolves |
| R5 Image N | Clip prompts label every plan entry (incl. multi-name combined cards) |
| R6 cap | Overflow mints `combined_cast`; uploads/product/scene reserved |
| R7 call packing | Dedicated tests stamp `output_ref` and assert `video_generation_overrides` paths |

---

## Suite shape

| Layer | Count | Notes |
|---|---|---|
| Parametric kinds | **40 × 1000 = 40000** | Topology + call mode + plan + prompt contracts |
| Fix / packing / stamp | ~40 | Dedicated intent injections |
| `test_user_references.py` | ~26 | Base64 materialize, rebase, attach, classify carry |

### Scenario kinds (1000 each)

**Core (20):**  
`product`, `motion`, `scene`, `character`, `text`, `character_family`, `character_condition_family`, `product_cast`, `scene_cast`, `motion_cast`, `id_reconcile`, `five_stills`, `stale_solo_flags`, `mixed_roles`, `two_character_stills`, `all_cast_covered`, `scene_condition_extra`, `product_and_scene`, `style_and_character`, `motion_stale_flags`

**video_binding / packing (20):**  
`story_motion_role_ignored`, `advertise_product_story`, `act_character_family`, `dinner_scene_story`, `animate_keyframe_solo`, `animate_keyframe_cast`, `packing_resolve_merge`, `image_n_labels`, `overflow_combined_cast`, `motion_role_without_binding`, `scene_product_cast_story`, `character_family_no_i2v`, `product_cast_image_labels`, `keyframe_condition_solo`, `keyframe_condition_cast`, `multi_ref_default_stamp`, `companion_edge_fallback`, `locked_scene_with_cast`, `style_authority_family`, `cap_prefers_uploads_and_combined`

---

## Kind → expected call mode (highlights)

| Kind | `video_binding` | Clip mode | Notes |
|---|---|---|---|
| `motion` / `animate_keyframe_solo` | animate_keyframe | **i2v** | Solo keyframe |
| `motion_cast` / `animate_keyframe_cast` | animate_keyframe | **r2v** | Keyframe + companions packed |
| `motion_role_without_binding` | (default multi_ref) | **r2v** | ROLE_MOTION alone must not I2V |
| `dinner_scene_story` / `story_motion_role_ignored` | multi_ref_story | **r2v** | Fixes live dinner I2V bug |
| `advertise_*` / `act_*` / `*_family` | multi_ref_story | **r2v** | Story verbs stay multi-ref |
| `overflow_combined_cast` | multi_ref_story | **r2v** | `combined_cast` on plan |

---

## Dedicated packing tests (must stay green)

- `test_stamp_persists_video_binding_default_multi_ref`  
- `test_motion_role_alone_does_not_force_i2v`  
- `test_dinner_scene_plus_motion_role_stays_r2v`  
- `test_r2v_merge_keeps_companion_paths_with_upload`  
- `test_compose_prompt_labels_every_plan_image`  
- `test_overflow_mints_combined_cast_card`  
- `test_animate_keyframe_cast_packs_refs_not_null`  

---

## Gaps / follow-ups

1. No live provider integration tests (MiniMax/Wan HTTP).  
2. Classify golden-set for `video_binding` calibration not yet checked in.  
3. Canvas UX choice (keep solo cards vs only combined) left to product; topology mints combined for Wan only when over cap.  

Pipeline contract: `reference-mode-full-pipeline.md`.  
