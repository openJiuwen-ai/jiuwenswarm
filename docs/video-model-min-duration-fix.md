# Video model duration capacity (all models) — change log

**Branch:** `0.2.8.beta1-VideoModelMinDurationFix-1.0`  
**Issue:** [openJiuwen-ai/jiuwenswarm#7699](https://github.com/openJiuwen-ai/jiuwenswarm/issues/7699) — MiniMax-H3-Max reject `duration: 4` (API 2013, supported 5–15s).  
**Approach:** Same as resolution: catalog + `snap_*` + Designer resolve + submit defense for **every** video model, not MiniMax-only.

---

## Policy (mirrors resolution)

| Layer | Resolution (existing) | Duration (this change) |
|---|---|---|
| Catalog | `VideoModelCapacity.resolutions` / `default_resolution` | `min_sec` / `max_sec` / `default_sec` (already in catalog; now **read**) |
| Snap | `snap_resolution(requested, capacity)` | **`snap_duration(requested, capacity)`** |
| Designer generate | `resolve_clip_video_format` → `active_video_capacity()` | **`resolve_clip_video_duration`** → same capacity |
| Submit | vendor remaps (MiniMax 768P/2K, etc.) | **`_snap_request_duration(target.model, …)`** on MiniMax, ModelArk, DashScope |
| Missing / invalid | model default tier | model **`default_sec`** |
| Out of range | nearest allowed tier | clamp to **`min_sec`–`max_sec`** |

**Not done:** raising global `_VIDEO_MIN_SECONDS` to 5 (that would break MiniMax-H3 and Seedance 4s). Those globals are **removed**.

---

## Per-model snap (catalog)

| Model | min | default | max | 4s request | 99s request |
|---|---|---|---|---|---|
| MiniMax-H3 | 4 | 5 | 15 | 4 | 15 |
| MiniMax-H3-Max | 5 | 6 | 15 | **5** | 15 |
| Seedance 2.0 | 4 | 5 | 15 | 4 | 15 |
| Seedance 2.5 | 4 | 5 | 30 | 4 | **30** (was wrongly capped at 15) |
| Wan 3.0 | 2 | 5 | 30 | 4 | 30 |
| Unknown id | Wan fallback | | | | |

---

## Files changed

### Production

| File | What changed |
|---|---|
| `jiuwenswarm/server/runtime/designer/pipeline/model_capacity.py` | Added **`snap_duration`**: invalid/missing → `default_sec`; else clamp `min_sec`–`max_sec`. Same module as `snap_resolution`. |
| `jiuwenswarm/server/runtime/designer/pipeline/axis_locks.py` | Added **`resolve_clip_video_duration`**, parallel to `resolve_clip_video_format`: uses `active_video_capacity()` + `snap_duration`. |
| `jiuwenswarm/server/runtime/designer/pipeline/clip_shot_scope.py` | `clamp_clip_duration` now delegates to `resolve_clip_video_duration` (capacity-aware, not `MIN_CLIP_SEC=2` only). `sequential_windows` uses active model `min_sec`/`max_sec` instead of hard-coded 2/15. |
| `jiuwenswarm/server/runtime/designer/handlers/clip.py` | `generate_clip_video` snaps duration via `resolve_clip_video_duration` next to `resolve_clip_video_format`. |
| `jiuwenswarm/server/runtime/designer/handlers/text_nodes.py` | Storyboard example no longer hard-codes `0.0-4.0s`; tells the director to respect the configured model's minimum. |
| `jiuwenswarm/agents/harness/common/tools/gen_toolkits.py` | Removed `_VIDEO_MIN_SECONDS` / `_VIDEO_MAX_SECONDS` / `_clamp_duration`. Added `_snap_request_duration(model, seconds)` using `capacity_for_model` + `snap_duration`. Wired into MiniMax submit, ModelArk submit, and DashScope `parameters.duration`. |
| `jiuwenswarm/agents/harness/common/tools/vllm_omni_gen.py` | Duration uses catalog snap. Split H3 vs **H3-Max** matchers (`minimax-h3` in id no longer treats Max as 4s H3). `VllmOmniVideoInputs.model_id` passed from submit. Generic form seconds/num_frames also snap. |

### Tests

| File | What changed |
|---|---|
| `tests/unit_tests/designer/test_resolution_choice.py` | `test_duration_snaps_like_resolution_per_model`, `test_resolve_clip_video_duration_uses_active_model`. |
| `tests/unit_tests/designer/test_clip_shot_scope.py` | `sequential_windows` test pins Wan via `configured_video_model_id`. |
| `tests/unit_tests/agents/test_gen_toolkits.py` | H3-Max 4→5 submit; H3 4 stays 4; Seedance 2.5 duration 25 allowed; Wan duration 1→2. |
| `tests/unit_tests/agents/harness/test_vllm_omni_gen.py` | H3 unspecified duration → default 5; 1→4; 30→15; H3-Max form 4→5. |

---

## Call path after the fix

```
Storyboard timeline (e.g. 0.0-4.0s)
  → duration_from_timeline / clamp_clip_duration
      → resolve_clip_video_duration (active Settings model)
  → generate_clip_video → resolve_clip_video_duration again
  → VideoRequest(duration_seconds=snapped)
  → gen_toolkits submit (MiniMax / ModelArk / DashScope)
      → _snap_request_duration(target.model, …)  # defense if agent bypassed Designer
  → POST duration is always in that model's catalog range
```

---

## What this does **not** change

- Resolution snapping and vendor remaps (unchanged).
- ComfyUI widget default `duration: 4` (local Wan UI; not cloud submit).
- Film splitting policy (still not forced to 15s).
- Reference-led topology / `video_binding` (other branch).

---

## How to verify

```bash
cd jiuwenswarm
.venv/Scripts/python.exe -m pytest \
  tests/unit_tests/designer/test_resolution_choice.py \
  tests/unit_tests/designer/test_clip_shot_scope.py \
  tests/unit_tests/agents/test_gen_toolkits.py \
  tests/unit_tests/agents/harness/test_vllm_omni_gen.py \
  -q --no-cov
```

Manual: configure **MiniMax-H3-Max**, plan a 4s clip, Play — submit body must be **`duration: 5`**, not API 2013.
