---
name: designer-scenario-video
description: Guide video graph composition and shot pipeline (R2V shots with clip-native sound).
---

# Designer Video Scenario Skill

## Goal
Compose a short cinematic pipeline: Brief → Character → Scene → Storyboard → Clips (R2V) → Compose.

## Graph creation rules
1. Always keep a Brief agent first.
2. Character and Scene sheets before Storyboard when subjects/places matter.
3. Storyboard must emit timed shots with camera, action, and shot prompts.
4. Each shot is an R2V shot from on-screen solos + scene specs (no per-shot keyframe required).
5. Compose/final stitches clips; honor audio policy from the brief.
6. Named director styles live in `metadata.video_style` (e.g. `final_frame_reverse` = reference still is the LAST 1s endpoint; reverse-form the action; see `skills/styles/`).

## Audio policy
- Do not create audio, speech, TTS, music, or BGM nodes.
- Keep requested dialogue or sound direction in clip-native video prompts.
- Users may manually add an Audio node, upload a file, and connect it to Compose.
- If user says **no sound / silent / mute / 无声**, tell clip/compose agents to avoid implied dialogue.

## Model capabilities to exploit
- Image: t2i and i2i/editing (character consistency).
- Video: reference-to-video (solos + scene specs).
- Audio/speech: use clip-native video audio when supported.

## Quality bar
Cinematic lighting, consistent identity, readable action, configured resolution clips, coherent continuity across shots.
