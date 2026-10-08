# Designer: toolbar user prompt lost at regenerate (Film-shot → Image-N gate)

**Branch:** `0.2.8.beta1-A-P0-Fixes`  
**Related:** user-edit-prompt authority (`prompt_origin=user`), OTLP `graph_de2fd58779266f1b.otlp.jsonl` (family-car / blue-car regen)  
**Status:** Fixed in code (this document describes problem + fix + trajectory delta)

---

## 1. Problem (what you saw)

1. Graph generated; clip nodes ran.
2. Regenerate toolbar showed a long **Film shot N** essay (often mid-word truncated at ~1400 chars).
3. You edited facts (e.g. **blue car**) and regenerated shot 1.
4. Video stayed grey / ignored the color.

### What the artifacts proved

| Surface | Contained “blue car”? |
|---------|------------------------|
| OTLP `designer_graph_get` result (`cfg.prompt` / `regenerate_packet.prompt`) | **Yes** |
| OTLP `call_video_model` / `video_generation` arguments | **No** — Image-N practice form, “family car” only |
| Workspace `designer_agent_run_*_n_clip_1.md` | **No** — same practice body |
| Saved graph after later clears | Often empty `generate.prompt` + `prompt_origin=user` |

So Wan/MiniMax did **not** ignore blue. The **programmatic director gate** never put blue into the tool body.

---

## 2. Root cause

### Dual prompt surfaces

| Layer | Role |
|-------|------|
| Toolbar / Film-shot / packet / `user_edit_prompt` | Human beat intent |
| `shot_action` / `camera` | Storyboard beats |
| `last_wan` / stamped `generate.prompt` | Last **sent** Image-N API body |

### Intercept point (not the LLM Director agent)

```
n_clip_1.execute
  → invoke_agent (leaf may narrate)
  → designer_graph_get          # still has Film-shot “blue car”
  → call_video_model
       → resolve_user_origin_prompt   # BUG: preferred stale generate.prompt
       → apply_wan_call_locks
            → director_approve_video_prompt
                 → director_prepare_video_prompt
                      → compose_practice_prompt   # Image-N rebuild
       → video_generation             # post-lock body, no blue car
```

### Specific bugs after A-P0 (`184a95e`)

1. **`resolve_user_origin_prompt` ordered `generate.prompt` first**  
   Prior practice stamp (empty-room / family car) won over packet Film-shot with blue car.

2. **`_stamp_sent_prompt` wrote the API body back into `regenerate_packet.prompt`**  
   Next regenerate treated the practice body as “user” authority.

3. **UI resolve vs backend resolve disagreed**  
   Toolbar could show packet Film-shot while the gate seeded from stale generate.

4. **Film-shot essays always fail practice checks** (missing Image-N, lock banners) → full compose rewrite. That is OK **if** action is seeded from the Film-shot text; it was seeded from the wrong surface.

Why the gate still exists (must not delete): Image-N ↔ attach order, wardrobe/seat, exited cast, no FORBID essays, length, speech, continuity. Leaf LLM is untrusted for those. Fix = **narrow authority**, not remove the gate.

---

## 3. Fix (what we changed)

### Strategy

- Keep programmatic structure gate.
- Make **durable user beat authority** (`user_edit_prompt` + packet) win over stamped API text.
- On rewrite, seed from that Film-shot / user surface; merge seed facts if compose drops nouns.
- Align toolbar write/read with the same authority.

### Files changed and why

| File | Why |
|------|-----|
| `jiuwenswarm/server/runtime/designer/pipeline/video_prompt_practice.py` | `resolve_user_origin_prompt`: order = `user_edit_prompt` → packet → `cfg.prompt` → `generate.prompt` → fallback; **never** `last_wan` / `last_approved` as beat authority. `narrative_seed_from_user_prompt`: cap 1200; strip inline CLOTHING/STRATEGY banners. |
| `jiuwenswarm/server/runtime/designer/pipeline/wan_call_locks.py` | `_stamp_sent_prompt`: preserve `user_edit_prompt` + packet as pre-rewrite user text; stamp `last_*` / `generate.prompt` with **sent** body only. Camera extract from Film-shot “Camera … Action:”. `_merge_user_seed_facts` if compose drops distinctive nouns. |
| `jiuwenswarm/server/runtime/designer/node_agent.py` | Before WAN gate, prefer `resolve_user_origin_prompt` over leaf narration when origin=user. |
| `jiuwenswarm/channels/web/frontend/src/features/designer/mediaNodeConfig.ts` | Toolbar write sets `user_edit_prompt` + packet; resolve prefers user_edit → packet → last_wan → … |
| `tests/unit_tests/designer/test_user_origin_wan_locks.py` | Packet-vs-stale-generate + stamp non-poison cases. |
| `tests/unit_tests/designer/test_user_prompt_edit_scenarios.py` | **20k** resolve + **40k** pipeline Film-shot edit survival (incl. second regen). |

---

## 4. Trajectory differences (before vs after)

OTLP key remains `~/.jiuwenswarm/.trace/designer/<graph_id>.otlp.jsonl` (append-only tool/agent spans). The **pipeline shape is unchanged**; **prompt authority inside spans** changes.

### Span order (unchanged)

```
n_clip_1.execute
  → invoke_agent
  → read_upstream
  → designer_graph_get
  → call_video_model
       → (nested) video_generation
  → designer_node_complete
```

### What changes inside those spans

| Span / field | Before (broken) | After (fixed) |
|--------------|-----------------|---------------|
| `designer_graph_get` result | Film-shot blue car in packet/`prompt`; stale practice in `generate.prompt` | Same shapes, plus durable `user_edit_prompt` = Film-shot |
| Authority chosen at gate | Often **stale `generate.prompt`** | **`user_edit_prompt` / packet** Film-shot |
| `call_video_model` args | Leaf/practice without blue car | User Film-shot (or seed-merged practice) carrying blue car |
| `video_generation` args | Practice “family car”, no blue | Practice (or kept) body **with blue car** (seeded/merged) |
| Post-run packet | Overwritten with API practice (poison) | **Still Film-shot / user_edit** |
| Post-run `last_wan` | Sent practice | Sent practice (honesty) |
| Next regenerate | Replays poisoned practice as “user” | Re-reads `user_edit_prompt` / packet |

### What OTLP still does **not** show

- Frontend `writeMediaGeneratePatch` / persistBeforeRun  
- Exact MiniMax HTTP wire body (closest: `video_generation` args)  
- Live disk graph after unrelated clears  

Reviewers should treat OTLP as ground truth for **backend tool envelopes**, and `user_edit_prompt` + `video_generation` args for **whether the edit survived the gate**.

---

## 5. Acceptance

- [x] Packet Film-shot with prop + stale generate practice → WAN approved text contains prop.  
- [x] Stamp keeps `user_edit_prompt` / packet; second regen still has prop.  
- [x] Storyboard origin path unchanged (no `kept_user_prompt` when origin ≠ user).  
- [x] Structure gate retained (Image-N / locks / length) **for video only**.  
- [ ] Manual: edit clip-1 Film-shot to blue car → regenerate → `video_generation` / video shows blue intent in prompt.

---

## 7. Still / image / other nodes (same authority, no hard-coded rewrite)

**Problem:** Scene regenerate toolbar showed truncated `One empty setting… SPATIAL LOCK… keyframes reuse` while `call_image_model` received a different rewritten plate. Hard-coded `ensure_still_tool_prompt` + `SPATIAL LOCK` prompt stamps overrode LLM/user text.

**Fix (image + character + frame/keyframe):**

| File | Change |
|------|--------|
| `wan_call_locks.apply_keyframe_call_locks` | Pass-through: `user_edit_prompt` → else LLM `prompt`; **no** `ensure_still` rewrite; stamp preserves user surfaces |
| `video_prompt_practice.resolve_user_origin_prompt` | Bare `user_edit_prompt` wins even if `prompt_origin` lagged |
| `resolve_still_call_prompt` | Still-call helper (user → LLM fallback; never `last_wan`) |
| `node_agent.call_image_model` | Prefer user surface before still call (mirrors video) |
| `handlers/image_nodes.py` | Character / Scene / Frame prefer user; skip `ensure_still` |
| `orchestration` director gate | Still path: user authority only; no `ensure_still` rewrite |
| `orchestration` spatial continuity | `spatial_lock` stays on **cfg only** — no `SPATIAL LOCK` essay appended to prompts (`[:1400]` cut removed) |
| `mediaNodeConfig.ts` | Toolbar skips displaying lock essays; falls through to `director_task` when needed |

**Policy difference vs video:** Video keeps the Image-N structure gate (vendor binding). Stills do **not** hard-rewrite — the leaf LLM / user prompt is sent as-is. Structured locks remain on node cfg for the agent to read.

---

## 6. Tests

```text
pytest tests/unit_tests/designer/test_user_origin_wan_locks.py --no-cov
pytest tests/unit_tests/designer/test_user_prompt_edit_scenarios.py::test_user_prompt_edit_resolve_scenario --no-cov
# 20_000 cases

pytest tests/unit_tests/designer/test_user_prompt_edit_scenarios.py::test_user_prompt_edit_pipeline_scenario --no-cov
# 40_000 cases

pytest tests/unit_tests/designer/test_user_prompt_still_scenarios.py --no-cov
# 20_000 resolve + 40_000 still pipeline + unit cases

pytest tests/unit_tests/designer/test_edit_prompt_authority_and_shot_scrub.py --no-cov
# existing edit-prompt authority + shot-reference scrub 20k+40k
```

### Results (this session)

| Suite | Count | Result | Time |
|-------|------:|--------|------|
| `test_user_prompt_edit_pipeline_scenario` | 40,000 | **passed** | 73.36s |
| `test_user_prompt_edit_resolve_scenario` + `test_edit_prompt_authority_and_shot_scrub` + `test_user_origin_wan_locks` | 80,007 | **passed** | 77.95s |
| (`test_user_origin_wan_locks` alone earlier) | 7 | **passed** | 0.39s |
| `test_user_prompt_still_scenarios` (resolve+pipeline+units) | 60,002 | **passed** | ~43s |
