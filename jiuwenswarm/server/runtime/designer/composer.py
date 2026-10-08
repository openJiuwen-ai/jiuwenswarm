# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Compose Designer graphs via the Director/Leader smart video pipeline."""

from __future__ import annotations

from jiuwenswarm.common.schema.designer_graph import DesignerExecutionGraph
from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata

_SCENARIO_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("3d", ("3d", "三维", "mesh", "glb", "blender", "texture", "pbr", "模型", "建模")),
    ("music", ("music", "song", "bgm", "soundtrack", "旋律", "音乐", "配乐", "作曲")),
    ("speech", ("podcast", "tts", "voiceover", "voice over", "配音", "旁白", "语音", "播客")),
    ("image", ("illustration", "poster", "logo", "封面", "插画", "海报", "still image")),
    ("video", ("video", "film", "movie", "cinematic", "trailer", "视频", "短片", "分镜", "动画")),
    # 「以及 / 同时」是普通连词，不能当成 multimodal。
    ("multimodal", ("and also", "both a video and", "pipeline", "全流程")),
]


def detect_scenario(prompt: str) -> str:
    text = prompt.lower()
    scores: dict[str, int] = {}
    for scenario, keywords in _SCENARIO_KEYWORDS:
        scores[scenario] = sum(1 for kw in keywords if kw in text)
    # Prefer video when cinematic/film cues appear alongside music/speech keywords.
    video_cues = (
        "video",
        "film",
        "movie",
        "cinematic",
        "trailer",
        "shot",
        "storyboard",
        "alley",
        "clip",
        "视频",
        "短片",
        "分镜",
        "动画",
        "油画",
        "原画",
        "画面",
        "致敬",
        "镜头",
        "关键帧",
    )
    if any(cue in text for cue in video_cues):
        scores["video"] = scores.get("video", 0) + 3
    best = max(scores, key=scores.get)
    if scores[best] <= 0:
        return "video"
    return best


def compose_execution_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
    scenario: str | None = None,
) -> DesignerExecutionGraph:
    """Bootstrap canvas via Director/Leader smart video pipeline.

    Catalog templates are a node library, not the workflow dumped onto the canvas.
    ``scenario`` is ignored; production always builds the video pipeline.
    """
    _ = scenario
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        analyze_creative_brief_sync,
    )
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        apply_runtime_delegate,
        build_smart_video_graph,
    )

    # Blunt LLM call — credential/billing failures raise DesignerLlmError.
    analysis = analyze_creative_brief_sync(prompt, timeout_sec=20.0)
    graph = build_smart_video_graph(
        project_id=project_id,
        prompt=prompt,
        analysis=analysis,
        title=title,
    )
    graph = apply_runtime_delegate(graph)
    meta = dict(graph.get("metadata") or {})
    meta["script_analysis"] = analysis
    meta["scenario"] = "video"
    graph["metadata"] = meta
    return attach_skills_metadata(graph, prompt)
