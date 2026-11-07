---
name: designer-scenario-video
description: Guide video graph composition and clip pipeline (R2V clips with clip-native sound).
---

# Designer Video Scenario Skill

## Goal
Compose a short cinematic pipeline: Brief → Character (**character_specs**) → Scene (**scene_specs**) → Storyboard → Clips (R2V) → Compose.

## Graph creation rules
1. Always keep a Brief agent first.
2. **Character_specs** and **scene_specs** before Storyboard when cast and settings matter.
3. Storyboard must emit a timed clip table: camera, action, and clip prompts per row.
4. Each row becomes one R2V **clip** from on-screen **character_specs** + **scene_specs**.
5. Compose/final stitches clips; honor audio policy from the brief.
6. Named director styles live in `metadata.video_style` (e.g. `final_frame_reverse` = reference image is the LAST ~1s endpoint of the arc; reverse-form the action; see `skills/styles/`).
7. Default path is **clip** agents only — no separate frame/keyframe leaf in the graph.

## Consistency
- **character_consistency**: face, body type, wardrobe locks from **character_specs**.
- **scene_consistency**: geography, light, props from **scene_specs**.
- **shot_consistency**: clip-to-clip continuity (blocking, last-frame handoff, speech anti-repeat) within the same setting.

## Audio policy
- Do not create audio, speech, TTS, music, or BGM nodes.
- Keep requested dialogue or sound direction in clip-native video prompts.
- Users may manually add an Audio node, upload a file, and connect it to Compose.
- If user says **no sound / silent / mute / 无声**, tell clip/compose agents to avoid implied dialogue.

## Model capabilities to exploit
- Image: t2i and i2i/editing (**character_consistency**).
- Video: reference-to-video (**character_specs** + **scene_specs**).
- Audio/speech: use clip-native video audio when supported.

## Quality bar
Cinematic lighting, consistent identity, readable action, configured resolution clips, coherent **shot_consistency** across the clip chain.
