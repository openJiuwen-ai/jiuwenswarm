# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Write Designer skill markdown into the in-repo designer skills package."""

from __future__ import annotations

import json
from pathlib import Path

ROOT = (
    Path(__file__).resolve().parents[1]
    / "jiuwenswarm"
    / "server"
    / "runtime"
    / "designer"
    / "skills"
)

SCENARIOS = {
    "video": """---
name: designer-scenario-video
description: Guide video graph composition and shot pipeline (R2V shots, optional speech/music/silence).
---

# Designer Video Scenario Skill

## Goal
Compose a short cinematic pipeline: Brief → Character → Scene → Storyboard → Clips (R2V) → Compose, with optional speech/music.

## Graph creation rules
1. Always keep a Brief agent first.
2. Character and Scene sheets before Storyboard when subjects/places matter.
3. Storyboard must emit timed shots with camera, action, and shot prompts.
4. Each shot is an R2V shot from on-screen solos + scene specs (no per-shot keyframe required).
5. Compose/final stitches clips; honor audio policy from the brief.
6. Named director styles live in `metadata.video_style` (e.g. `final_frame_reverse` = reference still is the LAST 1s endpoint; reverse-form the action; see `skills/styles/`).

## Audio policy
- If user says **no sound / silent / mute / 无声**: set `audio_intent.policy=silent`; do not add speech or music nodes; tell clip/compose agents to avoid implied dialogue.
- If user asks for **speech / voiceover / narration / 配音**: add Speech/TTS agent after storyboard; feed script into mix.
- If user asks for **music / BGM / 配乐**: add Music/bed agent; duck under speech if both exist.
- Default for unspecified video: optional soft bed, no forced dialogue.

## Model capabilities to exploit
- Image: t2i and i2i/editing (character consistency).
- Video: reference-to-video (solos + scene specs).
- Audio/speech: TTS when speech is requested.

## Quality bar
Cinematic lighting, consistent identity, readable action, configured resolution clips, coherent continuity across shots.
""",
    "image": """---
name: designer-scenario-image
description: Still-image / poster / illustration pipeline.
---

# Image Scenario Skill
Brief → concept → generate/edit → refine → final still.
Prefer 1K square unless user requests another aspect. Use subject guides for humans/products/vehicles.
""",
    "music": """---
name: designer-scenario-music
description: Music / BGM generation pipeline.
---

# Music Scenario Skill
Brief → motif → arrangement → mix → final audio.
Respect duration, mood, and silence gaps if marked.
""",
    "speech": """---
name: designer-scenario-speech
description: Speech / podcast / voiceover pipeline.
---

# Speech Scenario Skill
Script → voice select → TTS → denoise → bed → mix → chapters/final.
If user says no music bed, keep speech only.
""",
    "3d": """---
name: designer-scenario-3d
description: Mesh / texture / turntable style pipeline.
---

# 3D Scenario Skill
Brief → blocking mesh → materials → lighting → preview → export.
Preserve real-world scale and subject aspect conventions.
""",
    "multimodal": """---
name: designer-scenario-multimodal
description: Cross-modal package spanning image/video/audio/3d as needed.
---

# Multimodal Scenario Skill
Detect modalities from prompt; fan out to modality agents; sync via the director; assemble final package.
""",
}

ORCH = {
    "director": """---
name: designer-director
description: One overseer. Plans the graph, gates prompts, and writes the next-run improvement plan.
---

# Director Skill

You are the only Designer overseer.

## Planning
- Read scenario skill + prior feedback/trajectory.
- For each node set preferred_model and a concrete task.
- Enforce audio policy (silent / speech / music).
- Prefer R2V shots (on-screen solos + scene specs).

## Gates
- Approve the brief and storyboard before leaves run.
- Rewrite every leaf video prompt into concise story form.
- Prune nodes that do not contribute to compose.

## Rerun / rating
- When prior ratings are low, redesign weak nodes or reorder edges.
- Score each agent 0–10 with actionable suggestions.
- Write `improvement_plan` and per-node recommendations for the next Run.
- If speech was requested but missing, force speech nodes next run; if silent was requested but audio leaked, flag compose.
""",
}

AGENTS = {
    "brief": """# Brief Agent Skill
Turn user intent into an executable brief: logline, visual style, subjects, setting, duration, audio policy (speech/music/silent), constraints.
Detect subjects (human/vehicle/product/...) and note aspect guidance.
""",
    "character": """# Character Agent Skill
Produce a consistent character sheet. Apply subject guides (esp. human aspect ratios, costume, materials). Prefer identity-locking references for later i2i / R2V.
""",
    "scene": """# Scene Agent Skill
Establish place, weather, lighting, props. Keep continuity with brief. Provide references usable by clip agents.
""",
    "storyboard": """# Storyboard Agent Skill
Emit a timed camera table. Each shot needs: timeline, camera, move, action, scene change, and a clip prompt. Align actions to character sheet and place to scene sheet.
""",
    "frame": """# Keyframe / Still Agent Skill
Optional still for continuity debug. Match storyboard comment + character/scene consistency. Prefer readable silhouette and strong composition.
""",
    "clip": """# Clip Agent Skill
Generate shot video via **R2V** (on-screen solos + empty scene). Keep duration short. Honor silent policy (no implied dialogue) or leave room for later speech mix.
When `metadata.video_style=final_frame_reverse`: this shot sits on an arc that ENDS on the user reference / classic still — decisive motion early, settle late, motif-motivated continuity; never turntable a finished pose.
""",
    "compose": """# Compose / Final Agent Skill
Stitch clips, normalize resolution, apply audio mix policy: silent → no tracks; speech → voiceover; music → bed; both → duck music under speech.
""",
    "audio_bed": """# Audio Bed Agent Skill
Create non-vocal bed/SFX matching mood. Keep headroom for speech. Skip entirely when policy=silent.
""",
    "speech_tts": """# Speech / TTS Agent Skill
Write or refine spoken lines, choose voice, synthesize speech. Sync timing to storyboard. Skip when policy=silent or user forbids speech.
""",
    "music": """# Music Agent Skill
Compose motif/BGM for the requested mood and duration. Respect silent policy.
""",
    "mesh": """# Mesh / 3D Agent Skill
Block real-world scale assets; apply subject aspect conventions; prepare preview-friendly outputs.
""",
}

STYLES = {
    "final_frame_reverse": """# 终帧倒推 · 定格前最后几秒

DesignSwarm 视频生成风格之一（`video_style=final_frame_reverse`）。

## 目标

把经典名画、电影级定格图或角色定格图，改造成约 10–15 秒短视频。
核心不是讲完整故事，而是拍出「最终定格画面形成之前的最后几秒」。

## 公式

最终定格图作为终点 → 倒推出形成这张图之前的动作链 → 伪一镜到底建立空间和推进 → 关键特写强调神态、动作、物件 → 视觉母题完成转场 → 最后一秒收束为参考图构图。

## 硬规则

1. 参考图 / 名画构图 = 视频最后约 1 秒的终帧，不是开场。
2. 不要纯一镜到底：长镜头负责空间/气势/推进；特写负责神态、关键动作、关键物件与情绪落点。
3. 不要围绕已完成的静态画面旋转展示（禁止 turntable / 模型展示感）。
4. 每次切镜必须有视觉母题转场：烟尘、雨丝、旗布、羽毛、圣光、布料、枪火、人物擦镜、栏杆线条、倒影、云雾等。
5. 节奏：前段推进冲击与空间穿越；中段关键特写打点；后段减速归位；最后 1 秒定格对齐参考图。
6. 最后一枚 keyframe + 最后一镜 clip 必须收束到参考构图（姿态、取景、光线）。
7. 管线用每镜 R2V：角色单人板 + 空场景板；整片弧线的终点才是用户参考定格。

## Brief / Storyboard 写法

- 总体指令 → 核心动作链（倒推）→ 逐镜头时间轴（合计约 10–15 秒）
- 每镜写清：画面内容、镜头运动、角色动作、转场动机、情绪作用
- 负面提示 + 一句核心执行原则

## Clip（R2V）写法

- 明确本拍在整段弧线中的位置（前冲 / 中特写 / 后归位 / 终帧）
- 动作具体，禁止空泛「电影感」
- 承接上一镜的视觉母题
- 若为本片最后一拍：减速、归位、稳定，定格到参考图构图

## 负面

静态展示环绕、无动作链、无动机硬切、全程拖慢、结尾未对齐参考图、字幕水印。
""",
}

SUBJECTS = {
    "human": """# Human Subject Guide
- Portrait/character sheets: prefer 3:4 or 2:3 vertical; full-body turnaround 9:16 or 2:3.
- Keep head~1/7–1/8 body for adult; consistent eye line; avoid warped limbs.
- Costume/materials must persist across keyframes and clips.
""",
    "vehicle": """# Vehicle Subject Guide
- Side profile ~16:9; 3/4 front hero ~3:2; keep wheel/body proportions realistic.
- Preserve brand-agnostic silhouette consistency across shots.
""",
    "product": """# Product Subject Guide
- Hero packshot 1:1 or 4:5; floating product with soft reflections.
- Label text legible; consistent SKU colors for i2i edits.
""",
    "animal": """# Animal Subject Guide
- Match species proportions; fur/feather detail; eye catchlights.
- Prefer 3:2 for action; 1:1 for portrait cuteness.
""",
    "architecture": """# Architecture / Place Guide
- Establishing shots 16:9 or 2.39:1; interiors avoid extreme vertical stretch.
- Keep vanishing points stable across storyboard continuity.
""",
    "nature": """# Nature Guide
- Landscapes 16:9; macro details 1:1; weather/light continuity across clips.
""",
}


def write_map(subdir: str, mapping: dict[str, str]) -> None:
    directory = ROOT / subdir
    directory.mkdir(parents=True, exist_ok=True)
    for name, body in mapping.items():
        (directory / f"{name}.md").write_text(body.strip() + "\n", encoding="utf-8")


def main() -> None:
    ROOT.mkdir(parents=True, exist_ok=True)
    write_map("scenarios", SCENARIOS)
    write_map("orchestration", ORCH)
    write_map("agents", AGENTS)
    write_map("subjects", SUBJECTS)
    write_map("styles", STYLES)
    index = {
        "schema_version": "designer-skills-index.v1",
        "scenarios": sorted(SCENARIOS),
        "agents": sorted(AGENTS),
        "subjects": sorted(SUBJECTS),
        "orchestration": sorted(ORCH),
        "styles": sorted(STYLES),
    }
    (ROOT / "index.json").write_text(
        json.dumps(index, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )
    print("skills written", sum(1 for _ in ROOT.rglob("*.md")))


if __name__ == "__main__":
    main()
