# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The Director chain (creative request → execution graph) as one tool call.

This is the authoring half of the design layer, exposed to the leader as the
``designer_graph_autorun`` tool: the creative-request → execution-graph
pipeline that used to run as the canvas bootstrap. The chain's steps are NOT
reimplemented here — every step delegates to the ``Director`` methods in
``jiuwenswarm.server.runtime.designer.orchestration`` and to
``script_analysis.analyze_creative_brief``. This module only owns:

* step sequencing (media intent → analyze → reference reads → materialize
  graph → author brief → review brief → author storyboard → review
  storyboard → design execution graph → validate → delegate stamp);
* keeping the bound canvas identity (graph id, session, project, references,
  canvas media settings) across the Director's graph rebuilds;
* stage-progress reporting for the caller;
* error translation (``DesignerLlmError`` → ``DirectorChainError`` with a
  user-presentable message).

Module boundary — the design layer's three orchestration modules:

* ``director_chain``  — creative prompt → graph (this module; its LLM calls
  are independent service-side ``call_model_tool`` calls, NOT the leader's);
* ``graph_compiler``  — graph → swarmflow script;
* ``run_coordinator`` — trigger execution / lifecycle.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

# The cast/shot JSON grows with the story and is returned non-streamed, so the
# budget must cover the whole completion; later Director steps are bounded only
# by the 1200s client timeout in ``call_model_tool``.
_SCRIPT_ANALYSIS_TIMEOUT_SEC = 600.0

# Canvas identity the Director rebuilds would otherwise drop: each rebuild
# starts from ``build_smart_video_graph``'s fresh metadata.
_IDENTITY_META_KEYS = (
    "session_id",
    "project_dir",
    "project_name",
    "model_name",
)


class DirectorChainError(Exception):
    """Graph authoring chain failure with a user-presentable message."""

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.user_message = message


def _media_settings_for(graph: dict[str, Any], intent: Any) -> tuple[Any, Any]:
    """Canvas settings after a fresh request.

    The prompt's stated ratio / resolution win; anything the prompt leaves
    unstated keeps what the canvas already holds (toolbar edits or defaults).
    """
    from jiuwenswarm.server.runtime.designer.media_intent import media_settings_from_intent
    from jiuwenswarm.server.runtime.designer.media_settings import (
        ImageSettings,
        VideoSettings,
        image_settings,
        video_settings,
    )

    video_now = video_settings(graph)
    image_now = image_settings(graph)
    if not (intent.aspect_ratio or intent.resolution):
        return video_now, image_now
    video_intent, image_intent = media_settings_from_intent(intent)
    video = VideoSettings(
        aspect_ratio=video_intent.aspect_ratio if intent.aspect_ratio else video_now.aspect_ratio,
        resolution=video_intent.resolution if intent.resolution else video_now.resolution,
    )
    image = ImageSettings(
        aspect_ratio=image_intent.aspect_ratio if intent.aspect_ratio else image_now.aspect_ratio,
        resolution=image_now.resolution,
    )
    return video, image


def _restore_identity(
    graph: dict[str, Any],
    *,
    graph_id: str,
    project_id: str,
    identity: dict[str, Any],
    video: Any,
    image: Any,
) -> None:
    from jiuwenswarm.server.runtime.designer.media_settings import write_media_settings

    graph["graph_id"] = graph_id
    graph["project_id"] = project_id
    meta = dict(graph.get("metadata") or {})
    meta.update(identity)
    graph["metadata"] = meta
    write_media_settings(graph, video=video, image=image)


async def run_director_chain(
    graph: dict[str, Any],
    *,
    prompt: str,
    on_stage: Any | None = None,
) -> dict[str, Any]:
    """Run the Director chain for the bound canvas; returns the composed graph.

    ``graph`` is the session's canvas (possibly empty, possibly carrying user
    references). The composed graph keeps its graph id, project, session and
    references; the caller persists it. ``on_stage(kind, text[, tool])``
    receives per-stage progress.
    """
    from jiuwenswarm.server.runtime.designer.media_intent import extract_media_intent
    from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
    from jiuwenswarm.server.runtime.designer.orchestration import Director
    from jiuwenswarm.server.runtime.designer.pipeline.reference_led import (
        ReferenceIntentError,
        stamp_creative_intent,
    )
    from jiuwenswarm.server.runtime.designer.script_analysis import (
        analyze_creative_brief,
    )
    from jiuwenswarm.server.runtime.designer.skills_loader import attach_skills_metadata
    from jiuwenswarm.server.runtime.designer.smart_graph import (
        apply_runtime_delegate,
        build_smart_video_graph,
    )
    from jiuwenswarm.server.runtime.designer.user_references import (
        analysis_prompt_with_references,
        carry_user_references,
        classify_reference_images,
        graph_user_references,
        image_reference_records,
    )

    def _stage(kind: str, text: str, tool: str = "") -> None:
        if callable(on_stage):
            try:
                on_stage(kind, text, tool) if tool else on_stage(kind, text)
            except Exception:  # noqa: BLE001 — progress is best-effort
                logger.debug("graph authoring stage emit failed", exc_info=True)

    refs = graph_user_references(graph)
    prompt = str(prompt or "").strip() or ("根据参考素材创作" if refs else "")
    if not prompt:
        raise DirectorChainError("prompt 不能为空")

    source_meta = dict(graph.get("metadata") or {})
    graph_id = str(graph.get("graph_id") or "")
    project_id = str(graph.get("project_id") or "")
    identity = {
        key: source_meta[key]
        for key in _IDENTITY_META_KEYS
        if source_meta.get(key) not in (None, "")
    }
    image_refs = image_reference_records(refs)
    try:
        _stage("thinking", "Director · Reading video format")
        intent = await extract_media_intent(prompt)
        video, image = _media_settings_for(graph, intent)
        _stage("thinking", "Director · Extracting cast and scenes (LLM)")
        analysis = await analyze_creative_brief(
            analysis_prompt_with_references(prompt, refs),
            timeout_sec=_SCRIPT_ANALYSIS_TIMEOUT_SEC,
            reference_images=[str(item.get("path") or "") for item in image_refs],
            film_duration_sec=intent.film_duration_sec,
        )
        if not isinstance(analysis, dict) or str(analysis.get("source") or "") != "llm":
            raise DirectorChainError("模型未返回可用的角色/镜头分析")
        _stage(
            "stage",
            f"Director · LLM cast locked ({len(analysis.get('characters') or [])} characters)",
        )
        if image_refs:
            _stage("thinking", "Supervisor · Reading reference images")
            reads = await classify_reference_images(prompt, image_refs, analysis)
            if not reads:
                raise DirectorChainError("参考图无法判定用途，请重试")
            try:
                analysis = stamp_creative_intent(analysis, reads, image_refs)
            except ReferenceIntentError as exc:
                raise DirectorChainError(str(exc)) from exc

        _stage("tool_call", "Director · Materializing graph", "build_smart_video_graph")
        composed = apply_runtime_delegate(
            build_smart_video_graph(project_id=project_id, prompt=prompt, analysis=analysis)
        )
        meta = dict(composed.get("metadata") or {})
        meta["scenario"] = "video"
        meta["script_analysis"] = analysis
        meta["director_composed_on_bootstrap"] = True
        meta["freeze_clip_topology"] = True
        if isinstance(analysis.get("scene_locks"), dict) and analysis["scene_locks"]:
            meta["scene_locks"] = analysis["scene_locks"]
        composed["metadata"] = meta
        composed = attach_skills_metadata(composed, prompt)
        carry_user_references(source_meta, composed)
        _restore_identity(
            composed,
            graph_id=graph_id,
            project_id=project_id,
            identity=identity,
            video=video,
            image=image,
        )

        director = Director()
        _stage("thinking", "Director · Authoring brief (LLM)")
        await director.author_creative_brief(composed)
        _stage("thinking", "Director · Reviewing / approving brief")
        await director.review_brief(composed)
        _stage("thinking", "Director · Designing storyboard (LLM)")
        await director.author_storyboard(composed)
        _stage("thinking", "Director · Reviewing / approving storyboard")
        await director.review_storyboard(composed)
        _stage(
            "tool_call",
            "Director · Designing execution graph (LLM)",
            "design_execution_graph",
        )
        await director.design_execution_graph(composed)
        _stage("thinking", "Director · Validating / approving graph + locks")
        await director.validate_plan(composed)
        composed = apply_runtime_delegate(composed)
        _restore_identity(
            composed,
            graph_id=graph_id,
            project_id=project_id,
            identity=identity,
            video=video,
            image=image,
        )
        meta = dict(composed.get("metadata") or {})
        meta["director_composed_on_bootstrap"] = True
        composed["metadata"] = meta
    except DesignerLlmError as exc:
        raise DirectorChainError(str(exc.user_message or exc)) from exc
    except DirectorChainError:
        raise
    except Exception as exc:  # noqa: BLE001 — chain failures are tool errors
        logger.exception("graph authoring chain failed")
        raise DirectorChainError(f"建图链失败: {exc}") from exc

    cast_n = sum(
        1
        for n in (composed.get("nodes") or [])
        if str(n.get("id") or "").startswith("n_character")
    )
    clip_n = sum(
        1
        for n in (composed.get("nodes") or [])
        if str(n.get("id") or "").startswith("n_clip")
    )
    _stage("stage", f"Director · Approved — {cast_n} character specs, {clip_n} clips")
    return composed


__all__ = ["DirectorChainError", "run_director_chain"]
