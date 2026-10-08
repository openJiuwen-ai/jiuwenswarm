# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Detect chat/vision/video capabilities for Designer agents and rating modality."""

from __future__ import annotations

import logging
import os
from typing import Any

from jiuwenswarm.common.config import get_config, resolve_env_vars
from jiuwenswarm.common.schema.designer_graph import (
    DesignerExecutionGraph,
    DesignerGraphNode,
    node_pipeline,
    node_role,
)
from jiuwenswarm.server.runtime.designer.model_tools import list_configured_models

logger = logging.getLogger(__name__)

_VISION_NAME_HINTS = (
    "gpt-4o",
    "gpt-4.1",
    "gpt-4-turbo",
    "gemini",
    "claude-3",
    "claude-4",
    "claude-sonnet",
    "claude-opus",
    "qwen-vl",
    "qwen2-vl",
    "qwen2.5-vl",
    "llava",
    "vision",
    "-vl-",
    "vl-",
    "omni",
)

_VIDEO_NAME_HINTS = (
    "video",
    "qwen2.5-vl",
    "gemini",
    "gpt-4o",
)

_VISION_TOOLS = ("inspect_image", "visual_question_answering", "call_vision_model")
_VIDEO_TOOLS = ("inspect_video", "call_video_model", "visual_question_answering")

_MUSIC_NAME_HINTS = (
    "music",
    "bgm",
    "suno",
    "udio",
    "audio-gen",
    "soundtrack",
    "melody",
)


def _name_looks_multimodal(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _VISION_NAME_HINTS)


def _name_looks_video_capable(name: str) -> bool:
    n = (name or "").lower()
    return any(h in n for h in _VIDEO_NAME_HINTS)


def _vision_backend_configured() -> dict[str, Any]:
    """Fast check: models.vision or VISION_*/API_KEY present (no network)."""
    key = (os.environ.get("VISION_API_KEY") or os.environ.get("API_KEY") or "").strip()
    model = (os.environ.get("VISION_MODEL_NAME") or "").strip()
    cfg = get_config() or {}
    models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    vision_block = models.get("vision") if isinstance(models, dict) else None
    if isinstance(vision_block, dict):
        vision_mc = (
            vision_block.get("model_config")
            or vision_block.get("model_client_config")
            or {}
        )
        if isinstance(vision_mc, dict):
            if not key:
                key = resolve_env_vars(str(vision_mc.get("api_key") or "")).strip()
            if not model:
                model = resolve_env_vars(str(vision_mc.get("model_name") or "")).strip()
    ok = bool(key) and key.lower() not in {"sk-xxxxxxxxx", "your-api-key", ""}
    return {
        "available": ok,
        "model": model or None,
        "tool": "visual_question_answering" if ok else None,
    }


def _chat_models_capability() -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for m in list_configured_models():
        name = str(m.get("model_name") or m.get("id") or "")
        out.append(
            {
                "id": m.get("id"),
                "model_name": name,
                "text": True,
                "vision": _name_looks_multimodal(name),
                "video": _name_looks_video_capable(name),
                "music": any(h in name.lower() for h in _MUSIC_NAME_HINTS),
            }
        )
    return out


def _audio_backend_configured(kind: str) -> dict[str, Any]:
    """Detect a music backend from env or config (no network)."""
    del kind
    key = (
        os.environ.get("MUSIC_API_KEY")
        or os.environ.get("BGM_API_KEY")
        or os.environ.get("AUDIO_MUSIC_API_KEY")
        or ""
    ).strip()
    model = (
        os.environ.get("MUSIC_MODEL_NAME")
        or os.environ.get("BGM_MODEL_NAME")
        or ""
    ).strip()
    block_names = ("music", "bgm", "audio_music")
    tool = "call_music_model"

    cfg = get_config() or {}
    models = cfg.get("models") if isinstance(cfg.get("models"), dict) else {}
    for name in block_names:
        block = models.get(name) if isinstance(models, dict) else None
        if not isinstance(block, dict):
            continue
        mc = block.get("model_config") or block.get("model_client_config") or {}
        if isinstance(mc, dict):
            if not key:
                key = resolve_env_vars(str(mc.get("api_key") or "")).strip()
            if not model:
                model = resolve_env_vars(str(mc.get("model_name") or "")).strip()
    # Named chat/default models that look like speech/music also count.
    for m in list_configured_models():
        nid = f"{m.get('id') or ''} {m.get('model_name') or ''}".lower()
        if any(h in nid for h in _MUSIC_NAME_HINTS):
            if not model:
                model = str(m.get("model_name") or m.get("id") or "")
            # Treat presence of a named audio model as available even without a
            # separate key when the chat stack already has credentials.
            if not key:
                key = "named-model"
            break

    ok = bool(key) and key.lower() not in {"sk-xxxxxxxxx", "your-api-key", ""}
    return {"available": ok, "model": model or None, "tool": tool if ok else None}


def detect_audio_backends() -> dict[str, Any]:
    """Public helper for Designer audio backends.

    TTS is deliberately disabled: dialogue is generated only as clip-native
    audio. Keep the unavailable ``speech`` record for payload compatibility.
    """
    speech = {"available": False, "model": None, "tool": None}
    music = _audio_backend_configured("music")
    can_video_audio = False
    video_audio_model = ""
    try:
        from jiuwenswarm.server.runtime.designer.audio_locks import (
            configured_video_gen_model,
            video_model_supports_native_audio,
        )

        model = configured_video_gen_model()
        can_video_audio = bool(model) and video_model_supports_native_audio(model)
        video_audio_model = model if can_video_audio else ""
    except Exception:  # noqa: BLE001
        can_video_audio = False
        video_audio_model = ""
    return {
        "can_speech": False,
        "can_music": bool(music.get("available")),
        "can_video_audio": bool(can_video_audio),
        "video_audio_model": video_audio_model,
        "speech": speech,
        "music": music,
    }


def _tools_for_role(
    role: str,
    *,
    can_vision: bool,
    can_video: bool,
    can_speech: bool = False,
    can_music: bool = False,
) -> list[str]:
    base = ["call_model", "read_upstream", "write_artifact"]
    if role in {"character", "character_design", "scene", "frame", "keyframe", "image"}:
        tools = ["call_model", "read_upstream", "call_image_model"]
    elif role in {"clip", "video"}:
        tools = ["call_model", "read_upstream", "call_video_model"]
    elif role in {"compose", "film"}:
        tools = ["call_model", "read_upstream", "compose_timeline", "mix_audio"]
    elif role in {"speech", "tts"}:
        # Kept only for loading legacy graphs. Designer no longer exposes TTS.
        tools = ["call_model", "read_upstream", "write_artifact"]
    elif role in {"music", "audio", "audio_bed"}:
        tools = ["call_model", "read_upstream", "call_music_model"]
        if not can_music:
            tools = ["call_model", "read_upstream", "write_artifact"]
    elif role in {"director"}:
        tools = ["call_model", "read_upstream", "rate_nodes", "write_report"]
    else:
        tools = list(base)
    if can_vision and role in {
        "director",
        "character",
        "character_design",
        "scene",
        "frame",
        "keyframe",
        "clip",
        "compose",
        "image",
        "video",
    }:
        tools.append("inspect_image")
        tools.append("visual_question_answering")
    if can_video and role in {"director", "clip", "compose"}:
        tools.append("inspect_video")
    # de-dupe preserve order
    seen: set[str] = set()
    ordered: list[str] = []
    for t in tools:
        if t not in seen:
            seen.add(t)
            ordered.append(t)
    return ordered


def decide_modality_plan(graph: DesignerExecutionGraph) -> dict[str, Any]:
    """Director start decision: models + tools → rating modality per agent."""
    chat = _chat_models_capability()
    vision_backend = _vision_backend_configured()
    audio = detect_audio_backends()
    any_chat_vision = any(bool(m.get("vision")) for m in chat)
    can_vision = bool(vision_backend.get("available")) or any_chat_vision
    can_video = any(bool(m.get("video")) for m in chat)  # no dedicated video-VLM tool yet
    # Designer dialogue is clip-native. A globally configured speech backend
    # must not re-enable TTS nodes or tools on the canvas.
    can_speech = False
    can_music = bool(audio.get("can_music"))
    # Prefer tool-backed vision for ratings when configured.
    rating_tools: list[str] = ["call_model", "read_upstream", "rate_nodes", "write_report"]
    if can_vision:
        rating_tools.extend(["inspect_image", "visual_question_answering"])
    if can_video:
        rating_tools.append("inspect_video")

    if can_vision:
        global_mod = "multimodal"
        reason = (
            "Vision tool/model available — director may inspect images "
            "(and keyframes standing in for shots) when rating."
        )
    else:
        global_mod = "text_only"
        reason = (
            "No vision-capable model or inspect_image/VQA tool configured — "
            "rate from text status, messages, and shot labels only."
        )
    reason = f"{reason} TTS disabled; music backend={'yes' if can_music else 'no'}."

    agents: dict[str, Any] = {
        "director": {
            "role": "director",
            "models": chat,
            "tools": list(rating_tools),
            "rating_modality": global_mod,
            "can_vision": can_vision,
            "can_video": can_video,
            "can_speech": can_speech,
            "can_music": can_music,
        },
    }
    for node in graph.get("nodes") or []:
        nid = str(node.get("id") or "")
        if not nid:
            continue
        role = str(node_pipeline(node) or node_role(node) or (node.get("config") or {}).get("role") or "")
        tools = _tools_for_role(
            role,
            can_vision=can_vision,
            can_video=can_video,
            can_speech=can_speech,
            can_music=can_music,
        )
        if can_vision and role in {
            "character",
            "character_design",
            "scene",
            "frame",
            "keyframe",
            "clip",
            "compose",
        }:
            node_mod = "multimodal"
        else:
            node_mod = "text_only"
        agents[nid] = {
            "role": role,
            "models": chat,
            "tools": tools,
            "rating_modality": node_mod,
            "can_vision": can_vision,
            "can_video": can_video,
            "can_speech": can_speech,
            "can_music": can_music,
        }
        cfg = dict(node.get("config") or {})
        cfg["tools"] = tools
        cfg["rating_modality"] = node_mod
        cfg["modality_reason"] = reason[:400]
        # Legacy speech nodes are removed by Director; never promote them.
        if role in {"speech", "tts"}:
            cfg["force_handler"] = True
            cfg["delegate"] = "handler"
        elif role in {"music", "audio", "audio_bed"}:
            if can_music:
                cfg["force_handler"] = False
                cfg["delegate"] = "agent"
            else:
                cfg["force_handler"] = True
                cfg["delegate"] = "handler"
        node["config"] = cfg

    plan = {
        "schema_version": "designer-modality-plan.v1",
        "global_rating_modality": global_mod,
        "can_vision": can_vision,
        "can_video": can_video,
        "can_speech": can_speech,
        "can_music": can_music,
        "vision_backend": vision_backend,
        "audio_backends": audio,
        "chat_models": chat,
        "agents": agents,
        "reason": reason,
        "rating_tools": rating_tools,
    }
    meta = dict(graph.get("metadata") or {})
    meta["modality_plan"] = plan
    meta["rating_modality"] = global_mod
    meta["can_speech"] = can_speech
    meta["can_music"] = can_music
    graph["metadata"] = meta
    return plan


def path_from_output_ref(ref: dict[str, Any] | None) -> str | None:
    if not isinstance(ref, dict):
        return None
    uri = str(ref.get("uri") or "").strip()
    if not uri:
        return None
    if uri.startswith("file:"):
        from urllib.parse import unquote, urlparse

        parsed = urlparse(uri)
        path = unquote(parsed.path)
        if os.name == "nt" and path.startswith("/") and len(path) > 2 and path[2] == ":":
            path = path[1:]
        return path
    if os.path.isfile(uri):
        return uri
    return None


async def inspect_image_for_rating(image_path: str, question: str) -> str:
    """Call VQA when configured; empty string on failure (caller falls back to text)."""
    try:
        from jiuwenswarm.agents.harness.common.tools.image_tools import (
            visual_question_answering,
        )

        result = await visual_question_answering(image_path, question)
        return str(result or "")[:2000]
    except Exception as exc:  # noqa: BLE001
        logger.info("inspect_image_for_rating failed: %s", exc)
        return ""


def collect_rateable_image_paths(
    graph: DesignerExecutionGraph,
    node_states: dict[str, Any] | None,
    *,
    limit: int = 3,
) -> list[tuple[str, str]]:
    """(node_id, local_path) for completed image nodes — capped for speed."""
    states = node_states or {}
    pairs: list[tuple[str, str]] = []
    for node in graph.get("nodes") or []:
        nid = str(node.get("id") or "")
        role = str(node_pipeline(node) or node_role(node) or "")
        if role not in {
            "character_design",
            "character",
            "scene",
            "frame",
            "keyframe",
            "image",
        }:
            continue
        state = states.get(nid) or {}
        if str(state.get("status") or "") != "completed":
            continue
        path = path_from_output_ref(state.get("output_ref"))
        if path and os.path.isfile(path):
            pairs.append((nid, path))
        if len(pairs) >= limit:
            break
    return pairs
