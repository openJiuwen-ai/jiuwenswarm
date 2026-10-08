# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Register user-attached image / video / audio as Designer bootstrap sources.

Original files stay the visual/audio authority. Analysis and prompts only
assign ordered slots (image 1 / video 1 / audio 1); they do not replace the
media with a text summary.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
import tempfile
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

from jiuwenswarm.common.schema.designer_graph import (
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_SCENE,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_VIDEO,
    node_pipeline,
)
from jiuwenswarm.server.runtime.attachments.upload_storage import (
    safe_upload_filename,
    unique_upload_path,
)

logger = logging.getLogger(__name__)

DEFAULT_ROLE = "reference"
REFERENCE_NODE_PREFIX = "n_ref_"
# Roles that describe look-and-feel only. Everything else may carry a subject
# whose identity the cast cards must preserve instead of redesigning.
STYLE_ONLY_ROLES = frozenset(
    {"style", "composition", "mood", "palette", "background", "environment", "scene"}
)
KIND_IMAGE = "image"
KIND_VIDEO = "video"
KIND_AUDIO = "audio"
SUPPORTED_KINDS = frozenset({KIND_IMAGE, KIND_VIDEO, KIND_AUDIO})
MAX_REFS_BY_KIND = {KIND_IMAGE: 5, KIND_VIDEO: 1, KIND_AUDIO: 1}
MAX_INLINE_BYTES = 6 * 1024 * 1024

_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".jfif"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".avi", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"}
_MIME_SUFFIX = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/jpg": ".jpg",
    "image/webp": ".webp",
    "image/gif": ".gif",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/quicktime": ".mov",
    "video/webm": ".webm",
    "video/x-matroska": ".mkv",
    "audio/mpeg": ".mp3",
    "audio/mp3": ".mp3",
    "audio/wav": ".wav",
    "audio/x-wav": ".wav",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "audio/mp4": ".m4a",
}
_DATA_URI_RE = re.compile(r"^data:([^;,]+);base64,(.+)$", re.DOTALL | re.IGNORECASE)


class UserReferenceError(ValueError):
    """Raised when bootstrap references cannot be materialized."""

    def __init__(self, message: str, *, code: str = "BAD_REQUEST") -> None:
        super().__init__(message)
        self.code = code


def graph_user_references(graph: dict[str, Any] | None) -> list[dict[str, Any]]:
    meta = (graph or {}).get("metadata") if isinstance(graph, dict) else None
    raw = (meta or {}).get("user_references") if isinstance(meta, dict) else None
    if not isinstance(raw, list):
        return []
    return [item for item in raw if isinstance(item, dict)]


def prompt_slot_roster(refs: list[dict[str, Any]] | None) -> str:
    """Ordered slot labels for prompts; never include paths or internal ids."""
    counts = {KIND_IMAGE: 0, KIND_VIDEO: 0, KIND_AUDIO: 0}
    lines: list[str] = []
    for item in refs or []:
        kind = str(item.get("kind") or "").strip().lower()
        if kind not in SUPPORTED_KINDS:
            continue
        counts[kind] += 1
        filename = str(item.get("filename") or f"{kind}-{counts[kind]}").strip()
        lines.append(
            f"{kind} {counts[kind]} = {filename}, generic reference "
            "(visual/audio authority; take compatible visible traits)"
        )
    return "\n".join(lines)


def analysis_prompt_with_references(prompt: str, refs: list[dict[str, Any]] | None) -> str:
    text = (prompt or "").strip()
    roster = prompt_slot_roster(refs)
    if not roster:
        return text
    prefix = text or "根据参考素材创作"
    return (
        f"{prefix}\n\n"
        "REFERENCE_MEDIA (not a story beat; do not turn into a shot):\n"
        "Original files are the authority; do not reduce them to a style-only summary. "
        "Slots in attachment order:\n"
        f"{roster}"
    )


def user_reference_paths(graph: dict[str, Any] | None, *, kind: str) -> list[Path]:
    wanted = str(kind or "").strip().lower()
    suffixes = {
        KIND_IMAGE: _IMAGE_SUFFIXES,
        KIND_VIDEO: _VIDEO_SUFFIXES,
        KIND_AUDIO: _AUDIO_SUFFIXES,
    }.get(wanted, set())
    paths: list[Path] = []
    seen: set[str] = set()
    for item in graph_user_references(graph):
        if str(item.get("kind") or "").strip().lower() != wanted:
            continue
        path = _existing_file(str(item.get("path") or item.get("uri") or ""))
        if path is None or path.suffix.lower() not in suffixes:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        paths.append(path.resolve())
    return paths


def user_reference_image_paths(graph: dict[str, Any] | None) -> list[Path]:
    return user_reference_paths(graph, kind=KIND_IMAGE)


def wired_user_reference_images(ctx: Any, node: dict[str, Any] | None) -> list[Path]:
    """Upload stills that actually arrive on this node's incoming edges."""
    from jiuwenswarm.server.runtime.designer.handlers.clip import edge_image_flow

    flow = edge_image_flow(ctx, node if isinstance(node, dict) else None)
    if not flow:
        return []
    graph = ctx.graph if isinstance(getattr(ctx, "graph", None), dict) else None
    wanted = {str(path.resolve()) for path in user_reference_image_paths(graph)}
    found: list[Path] = []
    seen: set[str] = set()
    for _role, _label, path in flow:
        try:
            key = str(path.resolve())
        except OSError:
            continue
        if key not in wanted or key in seen:
            continue
        seen.add(key)
        found.append(path.resolve())
    return found


def identity_reference_image_paths(graph: dict[str, Any] | None) -> list[Path]:
    """Uploads that may depict a subject, so cast cards must not redesign them."""
    style_only: set[str] = set()
    for item in graph_user_references(graph):
        if str(item.get("kind") or "").strip().lower() != KIND_IMAGE:
            continue
        if str(item.get("role") or DEFAULT_ROLE).strip().lower() not in STYLE_ONLY_ROLES:
            continue
        candidate = _existing_file(str(item.get("path") or item.get("uri") or ""))
        if candidate is not None:
            style_only.add(str(candidate.resolve()))
    return [p for p in user_reference_image_paths(graph) if str(p) not in style_only]


def user_reference_video_path(graph: dict[str, Any] | None) -> Path | None:
    paths = user_reference_paths(graph, kind=KIND_VIDEO)
    return paths[0] if paths else None


def user_reference_audio_path(graph: dict[str, Any] | None) -> Path | None:
    paths = user_reference_paths(graph, kind=KIND_AUDIO)
    return paths[0] if paths else None


async def classify_reference_images(
    prompt: str,
    refs: list[dict[str, Any]] | None,
    analysis: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Ask the model what each uploaded still is, before any edge is chosen.

    A person, a place, a product, and a motion frame are different destinations.
    Default binding is use-as-is (verbatim); this call only returns that judgment.
    """
    slots: list[dict[str, Any]] = []
    images: list[str] = []
    for item in refs or []:
        if str(item.get("kind") or "").strip().lower() != KIND_IMAGE:
            continue
        path = str(item.get("path") or item.get("uri") or "").strip()
        if not path:
            continue
        slots.append(item)
        images.append(path)
    if not images:
        return []
    roster = [
        f"image {index} = {str(item.get('filename') or f'image-{index}')}"
        for index, item in enumerate(slots, start=1)
    ]
    cast_roster: list[dict[str, Any]] = []
    set_roster: list[dict[str, Any]] = []
    if isinstance(analysis, dict):
        for item in analysis.get("characters") or []:
            if not isinstance(item, dict):
                continue
            cast_roster.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                    "match_terms": [
                        str(t).strip()
                        for t in (item.get("match_terms") or [])
                        if str(t).strip()
                    ],
                }
            )
        for item in analysis.get("scenes") or []:
            if not isinstance(item, dict):
                continue
            set_roster.append(
                {
                    "id": str(item.get("id") or "").strip(),
                    "name": str(item.get("name") or "").strip(),
                }
            )
    system = (
        "You inspect each attached reference image together with the user's full "
        "request, and judge what the user wants done with that exact still. "
        "DEFAULT for every role is use-as-is: binding='verbatim'. Only choose "
        "binding='condition' when the user clearly asks to restyle, redraw, "
        "decompose, or generate a new identity sheet / set plate from the still. "
        "Output ONLY one JSON object: "
        '{"reference_reads":[{"slot":1,"subject":"object","roles":["product_hero"],'
        '"binding":"verbatim","video_binding":"multi_ref_story",'
        '"style_authority":false,"set_lock":false,'
        '"motion_source":false,"medium":"","look":"","palette":"",'
        '"character_id":"","setting_id":"","rationale":""}]}. '
        "One read per image; slot numbers follow the roster order. "
        "roles is a list. Use character_identity for a person who will perform, "
        "scene_source for a place, product_hero for an item that must stay as itself, "
        "still_motion_source only when this image is the opening keyframe for a "
        "true animate-this-frame job, style_source when only the medium and palette "
        "should be copied. subject remains character, scene, or object for compatibility.\n"
        "Decide these fields from the user's intent and wording in ANY language — "
        "reason about meaning, never match fixed phrases or slogan banks:\n"
        "- video_binding (required, job-level intent; put the same value on each read): "
        "'multi_ref_story' (DEFAULT) for advertise / act / film / dinner / celebrate / "
        "product shot / story with identity or set references — clips use R2V and keep "
        "all uploads + generated cast/set as reference images. "
        "'animate_keyframe' ONLY when the user wants that still itself animated as the "
        "video's opening frame (e.g. animate this picture / painting / photo as the clip). "
        "Story verbs alone must NOT select animate_keyframe.\n"
        "- binding: prefer 'verbatim' (file itself is the reference card; no "
        "image-gen pass). Use 'condition' ONLY for an explicit restyle / redraw / "
        "new sheet / new plate request. Advertise / act / dinner / product shot while "
        "keeping the look = character_identity|scene_source|product_hero + verbatim + "
        "video_binding=multi_ref_story. Add still_motion_source only together with "
        "video_binding=animate_keyframe when the still IS the frame to animate.\n"
        "- set_lock: true when this still must stay the exact environment geometry of "
        "the film (the user is staging the story or ad inside this place). For a "
        "scene used as-is this is usually true.\n"
        "- style_authority: true when the film's medium/look should follow this "
        "still's rendering (typical for as-is character/scene/product). "
        "Leave it false only when the user named a competing medium in text "
        "(e.g. a photoreal live-action ad over a cartoon still) so their words win.\n"
        "- character_id / setting_id: copy an id from the analysis cast/set roster "
        "when the still depicts that person or place. Prefer roster ids over new names.\n"
        "- medium/look/palette: if you can see the picture, name its medium (e.g. "
        "anime, cartoon, photoreal, oil painting), its look, and palette. Leave them "
        "empty if you cannot see pixels — do NOT guess a medium from topic words.\n"
        "- rationale: one short clause explaining the binding and video_binding."
    )
    payload = {
        "user_prompt": (prompt or "")[:2000],
        "images": roster,
        "analysis_characters": cast_roster,
        "analysis_scenes": set_roster,
    }
    try:
        from jiuwenswarm.server.runtime.designer.model_tools import (
            call_model_tool,
            model_text_or_raise,
        )
        from jiuwenswarm.server.runtime.designer.script_analysis import (
            _extract_json_object,
            _reference_reads,
        )

        body = json.dumps(payload, ensure_ascii=False)

        async def _ask(*, with_images: bool) -> list[dict[str, Any]]:
            result = await call_model_tool(
                prompt=body,
                system=system,
                max_tokens=1024,
                images=images if with_images else None,
            )
            if result.get("unavailable") or result.get("ok") is False or result.get("fallback"):
                logger.info("reference image classification failed: %s", result.get("error"))
                return []
            parsed = _extract_json_object(model_text_or_raise(result)) or {}
            return _reference_reads(parsed if isinstance(parsed, dict) else {})

        reads = await _ask(with_images=True)
        if reads:
            return reads
        reads = await _ask(with_images=False)
        if not reads:
            logger.info("reference image classification returned no route for %s", roster)
        return reads
    except Exception as exc:  # noqa: BLE001
        logger.info("reference image classification failed: %s", exc)
        return []


def attach_user_references_to_graph(
    graph: dict[str, Any],
    refs: list[dict[str, Any]],
) -> dict[str, Any]:
    stored = [_public_record(item, index) for index, item in enumerate(refs, start=1)]
    meta = dict(graph.get("metadata") or {})
    meta["user_references"] = stored
    graph["metadata"] = meta
    roster = prompt_slot_roster(stored)
    ids = [str(item["id"]) for item in stored]
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) != NODE_ROLE_BRIEF:
            continue
        cfg = dict(node.get("config") or {})
        cfg["user_reference_ids"] = ids
        task = str(cfg.get("director_task") or "").strip()
        if roster:
            extra = (
                " User attached reference media; original files are visual/audio "
                f"authority. Slots:\n{roster}"
            )
            cfg["director_task"] = f"{task}{extra}".strip() if task else extra.strip()
        node["config"] = cfg
        break
    attach_user_reference_nodes(graph)
    return graph


def carry_user_references(
    source_meta: dict[str, Any] | None,
    rebuilt: dict[str, Any] | None,
) -> list[str]:
    """Re-seed uploads onto a rebuilt graph.

    ``build_smart_video_graph`` starts from a fresh metadata dict, so a
    Director redesign or shot re-expansion would otherwise drop the user's
    attachments and their canvas nodes.
    """
    if not isinstance(rebuilt, dict):
        return []
    refs = (source_meta or {}).get("user_references") if isinstance(source_meta, dict) else None
    refs = [item for item in refs if isinstance(item, dict)] if isinstance(refs, list) else []
    if not refs:
        return []
    attach_user_references_to_graph(rebuilt, refs)
    return user_reference_node_ids(rebuilt)


def user_reference_node_ids(graph: dict[str, Any] | None) -> list[str]:
    """Canvas nodes that stand for a user upload (never regenerated)."""
    out: list[str] = []
    for node in (graph or {}).get("nodes") or []:
        if not isinstance(node, dict):
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        if str(cfg.get("user_reference_id") or "").strip():
            out.append(str(node.get("id") or ""))
    return [nid for nid in out if nid]


def is_user_reference_node(node: dict[str, Any] | None) -> bool:
    """True for immutable canvas nodes backed by a user upload."""
    if not isinstance(node, dict):
        return False
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return bool(str(cfg.get("user_reference_id") or "").strip())


def is_uploaded_media_node(node: dict[str, Any] | None) -> bool:
    """True when a canvas media node is backed by user-uploaded content."""
    if not isinstance(node, dict):
        return False
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    if is_user_reference_node(node):
        return True
    if str(cfg.get("interaction_mode") or "").strip() != "upload":
        return False
    upload = cfg.get("upload") if isinstance(cfg.get("upload"), dict) else {}
    output_ref = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
    return bool(
        str(upload.get("uri") or "").strip()
        or str(output_ref.get("uri") or "").strip()
    )


def user_reference_node_file(node: dict[str, Any] | None) -> Path | None:
    """Resolve the uploaded file behind a reference node."""
    cfg = (node or {}).get("config") if isinstance(node, dict) else None
    cfg = cfg if isinstance(cfg, dict) else {}
    candidate = _existing_file(str(cfg.get("user_reference_path") or ""))
    if candidate is not None:
        return candidate.resolve()
    upload = cfg.get("upload") if isinstance(cfg.get("upload"), dict) else {}
    candidate = _existing_file(str(upload.get("uri") or ""))
    if candidate is not None:
        return candidate.resolve()
    ref_uri = ""
    output_ref = (node or {}).get("output_ref") if isinstance(node, dict) else None
    if isinstance(output_ref, dict):
        ref_uri = str(output_ref.get("uri") or "")
    candidate = _existing_file(ref_uri)
    return candidate.resolve() if candidate is not None else None


def _config_ids(node: dict[str, Any], *keys: str) -> set[str]:
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    found: set[str] = set()
    for key in keys:
        raw = cfg.get(key)
        if isinstance(raw, list):
            found.update(str(item).strip() for item in raw if str(item).strip())
        elif str(raw or "").strip():
            found.add(str(raw).strip())
    return found


def _reference_route(graph: dict[str, Any], item: dict[str, Any], image_slot: int) -> dict[str, Any]:
    """Route one image from the supervisor's look at that file, not from its kind."""
    analysis = (graph.get("metadata") or {}).get("script_analysis")
    reads = analysis.get("reference_reads") if isinstance(analysis, dict) else None
    if isinstance(reads, list):
        for read in reads:
            if not isinstance(read, dict):
                continue
            try:
                slot = int(read.get("slot") or 0)
            except (TypeError, ValueError):
                continue
            if slot == image_slot:
                return read
    subject = str(item.get("subject") or "").strip().lower()
    if subject in {"character", "scene", "object"}:
        return {
            "slot": image_slot,
            "subject": subject,
            "character_id": str(item.get("character_id") or "").strip(),
            "setting_id": str(item.get("setting_id") or "").strip(),
        }
    return {}


def _image_destinations(
    route: dict[str, Any],
    *,
    cast_nodes: list[dict[str, Any]],
    scene_nodes: list[dict[str, Any]],
    clip_ids: list[str],
) -> list[str]:
    """Person → character sheet, place → scene card, prop → clip generation."""
    subject = str(route.get("subject") or "").strip().lower()
    if subject == "character":
        wanted = str(route.get("character_id") or "").strip()
        matched = [
            str(node.get("id"))
            for node in cast_nodes
            if wanted and wanted in _config_ids(node, "character_id", "character_ids")
        ]
        if matched:
            return matched
        if len(cast_nodes) == 1 and str(cast_nodes[0].get("id") or ""):
            return [str(cast_nodes[0].get("id"))]
        return []
    if subject == "scene":
        wanted = str(route.get("setting_id") or "").strip()
        matched = [
            str(node.get("id"))
            for node in scene_nodes
            if wanted and wanted in _config_ids(node, "setting_id")
        ]
        if matched:
            return matched
        if len(scene_nodes) == 1 and str(scene_nodes[0].get("id") or ""):
            return [str(scene_nodes[0].get("id"))]
        return []
    if subject == "object":
        return list(clip_ids)
    return []


def attach_user_reference_nodes(graph: dict[str, Any]) -> list[str]:
    """Show each upload as its own canvas node.

    The node is a passthrough asset: the original file is the authority, so the
    runner must never regenerate it. An image is wired onward only after the
    supervisor has looked at it: a person goes to the matching character sheet,
    a place goes to the matching scene card, and a prop goes into clip generation.
    Video and audio stay on Brief.
    """
    refs = graph_user_references(graph)
    if not refs:
        return []
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    edges = [e for e in (graph.get("edges") or []) if isinstance(e, dict)]
    existing = {str(n.get("id") or "") for n in nodes}
    edge_ids = {str(e.get("id") or "") for e in edges}
    brief_ids = [
        str(n.get("id"))
        for n in nodes
        if node_pipeline(n) == NODE_ROLE_BRIEF and str(n.get("id") or "")
    ]
    cast_nodes = [
        n
        for n in nodes
        if node_pipeline(n) == NODE_ROLE_CHARACTER_DESIGN and str(n.get("id") or "")
    ]
    scene_nodes = [
        n
        for n in nodes
        if node_pipeline(n) == NODE_ROLE_SCENE and str(n.get("id") or "")
    ]
    clip_ids = [
        str(n.get("id"))
        for n in nodes
        if node_pipeline(n) == NODE_ROLE_CLIP and str(n.get("id") or "")
    ]
    cast_ids = {str(n.get("id")) for n in cast_nodes}
    scene_ids = {str(n.get("id")) for n in scene_nodes}
    node_type_by_kind = {
        KIND_IMAGE: NODE_TYPE_IMAGE,
        KIND_VIDEO: NODE_TYPE_VIDEO,
        KIND_AUDIO: NODE_TYPE_AUDIO,
    }
    added: list[str] = []
    wired_edges = False
    image_slot = 0
    for order, item in enumerate(refs, start=1):
        kind = str(item.get("kind") or KIND_IMAGE).strip().lower()
        node_type = node_type_by_kind.get(kind)
        path = _existing_file(str(item.get("path") or item.get("uri") or ""))
        if node_type is None or path is None:
            continue
        ref_id = str(item.get("id") or f"ref_{order:02d}")
        node_id = f"{REFERENCE_NODE_PREFIX}{order:02d}"
        subject = ""
        route: dict[str, Any] = {}
        if kind == KIND_IMAGE:
            image_slot += 1
            route = _reference_route(graph, item, image_slot)
        subject = str(route.get("subject") or "").strip().lower()
        if subject:
            item["subject"] = subject
            if str(route.get("character_id") or "").strip():
                item["character_id"] = str(route.get("character_id")).strip()
            if str(route.get("setting_id") or "").strip():
                item["setting_id"] = str(route.get("setting_id")).strip()
        resolved = path.resolve()
        filename = str(item.get("filename") or resolved.name)
        mime = str(item.get("mime_type") or _guess_mime(kind, resolved.suffix))
        if node_id in existing:
            for node in nodes:
                if str(node.get("id") or "") != node_id:
                    continue
                cfg = dict(node.get("config") or {})
                cfg.pop("user_added", None)
                cfg.pop("kind", None)
                cfg["delegate"] = "handler"
                cfg["force_handler"] = True
                cfg["skip_llm"] = True
                cfg["read_only"] = True
                cfg["immutable_source"] = True
                # Rebase onto the project-materialized file when Enter copied
                # base64/temp analysis paths into ``.designer/refs``.
                cfg["user_reference_id"] = ref_id
                cfg["user_reference_kind"] = kind
                cfg["user_reference_path"] = str(resolved)
                cfg["interaction_mode"] = "upload"
                upload = dict(cfg.get("upload") or {})
                upload.update(
                    {"filename": filename, "asset_id": ref_id, "mime_type": mime}
                )
                cfg["upload"] = upload
                cfg["materials"] = [
                    {
                        "id": ref_id,
                        "filename": filename,
                        "mime_type": mime,
                        "uri": resolved.as_uri(),
                    }
                ]
                if subject:
                    cfg["user_reference_subject"] = subject
                node["config"] = cfg
                node["output_ref"] = {
                    "kind": node_type,
                    "uri": resolved.as_uri(),
                    "mime_type": mime,
                    "label": filename,
                }
                break
        if node_id not in existing:
            ref_config: dict[str, Any] = {
                "role": node_type,
                "user_reference_id": ref_id,
                "user_reference_kind": kind,
                "user_reference_path": str(resolved),
                "interaction_mode": "upload",
                "upload": {
                    "filename": filename,
                    "asset_id": ref_id,
                    "mime_type": mime,
                },
                "materials": [
                    {
                        "id": ref_id,
                        "filename": filename,
                        "mime_type": mime,
                        "uri": resolved.as_uri(),
                    }
                ],
                "inputs": [],
                "delegate": "handler",
                "force_handler": True,
                "skip_llm": True,
                "read_only": True,
                "immutable_source": True,
                "director_task": (
                    "User-attached reference. Keep the original file as the "
                    "visual/audio authority; never regenerate or restyle it."
                ),
            }
            if subject:
                ref_config["user_reference_subject"] = subject
            nodes.append(
                {
                    "id": node_id,
                    "type": node_type,
                    "label": f"Reference {order}: {filename}",
                    "config": ref_config,
                    "layout": {
                        "x": -300.0,
                        "y": float(120 + (order - 1) * 180),
                        "width": 220,
                        "height": 140,
                    },
                    "output_ref": {
                        "kind": node_type,
                        "uri": resolved.as_uri(),
                        "mime_type": mime,
                        "label": filename,
                    },
                }
            )
            existing.add(node_id)
            added.append(node_id)
        targets = list(brief_ids)
        if kind == KIND_IMAGE and route:
            targets.extend(
                _image_destinations(
                    route,
                    cast_nodes=cast_nodes,
                    scene_nodes=scene_nodes,
                    clip_ids=clip_ids,
                )
            )
        for target in targets:
            edge_id = f"e_{node_id}_{target}"
            if edge_id in edge_ids:
                continue
            edges.append(
                {"id": edge_id, "source": node_id, "target": target, "kind": "data"}
            )
            edge_ids.add(edge_id)
            wired_edges = True
            for node in nodes:
                if str(node.get("id") or "") != target:
                    continue
                cfg = dict(node.get("config") or {})
                inputs = [str(x) for x in (cfg.get("inputs") or [])]
                if node_id not in inputs:
                    inputs.append(node_id)
                cfg["inputs"] = inputs
                task = str(cfg.get("director_task") or "").strip()
                hint = ""
                if target in cast_ids:
                    cfg["character_source_reference"] = ref_id
                    hint = (
                        " A user reference image of this person is wired in. "
                        "Derive the character sheet from it (face, wardrobe, palette) "
                        "instead of inventing a look."
                    )
                elif target in scene_ids:
                    hint = (
                        " A user reference image of this place is wired in. "
                        "Derive the scene card from it instead of inventing a location."
                    )
                elif target in clip_ids:
                    hint = (
                        " A user reference image of a prop is wired into this clip. "
                        "Pass that file in as a reference image when generating the shot."
                    )
                if hint and hint.strip() not in task:
                    cfg["director_task"] = f"{task}{hint}".strip()
                node["config"] = cfg
    if not added and not wired_edges:
        return []
    graph["nodes"] = nodes
    graph["edges"] = edges
    meta = dict(graph.get("metadata") or {})
    meta["user_reference_nodes"] = user_reference_node_ids(graph)
    graph["metadata"] = meta
    return added


def reapply_user_reference_routes(
    graph: dict[str, Any] | None,
    previous: dict[str, Any] | None = None,
) -> dict[str, Any] | None:
    """Put a classified upload back on its clip, character, or scene edge.

    A later save can rewrite the graph from an older copy and drop those edges.
    The judgment already stored on the upload or the previous graph is enough
    to draw them again.
    """
    if not isinstance(graph, dict) or not graph_user_references(graph):
        return graph
    meta = dict(graph.get("metadata") or {})
    refs = [
        dict(item) if isinstance(item, dict) else item
        for item in (meta.get("user_references") or [])
    ]
    previous_refs = {
        str(item.get("id") or ""): item
        for item in graph_user_references(previous)
        if str(item.get("id") or "")
    }
    for item in refs:
        if not isinstance(item, dict):
            continue
        if str(item.get("subject") or "").strip().lower() in {"character", "scene", "object"}:
            continue
        prev = previous_refs.get(str(item.get("id") or ""))
        if not isinstance(prev, dict):
            continue
        subject = str(prev.get("subject") or "").strip().lower()
        if subject not in {"character", "scene", "object"}:
            continue
        item["subject"] = subject
        if str(prev.get("character_id") or "").strip():
            item["character_id"] = str(prev.get("character_id")).strip()
        if str(prev.get("setting_id") or "").strip():
            item["setting_id"] = str(prev.get("setting_id")).strip()
    meta["user_references"] = refs
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    analysis = dict(analysis)
    if not analysis.get("reference_reads"):
        prev_meta = previous.get("metadata") if isinstance(previous, dict) else {}
        prev_analysis = (
            prev_meta.get("script_analysis") if isinstance(prev_meta, dict) else {}
        )
        prev_reads = prev_analysis.get("reference_reads") if isinstance(prev_analysis, dict) else None
        if isinstance(prev_reads, list) and prev_reads:
            analysis["reference_reads"] = prev_reads
            meta["script_analysis"] = analysis
    graph["metadata"] = meta
    attach_user_reference_nodes(graph)
    return graph


async def ensure_user_reference_routes(graph: dict[str, Any] | None) -> list[str]:
    """Recreate a missing reference node and wire it from the model's read.

    Play can meet a graph whose upload was pruned, or whose first analysis
    never said what the picture was. Classify only when that judgment is missing.
    """
    if not isinstance(graph, dict):
        return []
    refs = graph_user_references(graph)
    if not refs:
        return []
    meta = dict(graph.get("metadata") or {})
    analysis = meta.get("script_analysis") if isinstance(meta.get("script_analysis"), dict) else {}
    analysis = dict(analysis)
    covered: set[int] = set()
    for read in analysis.get("reference_reads") or []:
        if not isinstance(read, dict):
            continue
        try:
            covered.add(int(read.get("slot") or 0))
        except (TypeError, ValueError):
            continue
    slot = 0
    missing = False
    for item in refs:
        if str(item.get("kind") or "").strip().lower() != KIND_IMAGE:
            continue
        slot += 1
        if str(item.get("subject") or "").strip().lower() in {"character", "scene", "object"}:
            continue
        if slot in covered:
            continue
        missing = True
        break
    if missing:
        prompt = str(
            meta.get("user_prompt") or meta.get("prompt") or meta.get("approved_brief") or ""
        )
        reads = await classify_reference_images(prompt, refs, analysis)
        if reads:
            analysis["reference_reads"] = reads
            meta["script_analysis"] = analysis
            graph["metadata"] = meta
    return attach_user_reference_nodes(graph)


def normalize_user_references(
    raw: Any,
    *,
    dest_dir: Path | None,
) -> list[dict[str, Any]]:
    """Copy / decode attachments into dest_dir and return ordered source records.

    When ``dest_dir`` is None, return lightweight preview records (no disk copy)
    suitable for LLM analysis before the project directory exists.
    """
    if raw in (None, ""):
        return []
    if not isinstance(raw, list):
        raise UserReferenceError("references must be an array")
    preview_only = dest_dir is None
    if not preview_only:
        dest_dir = Path(dest_dir)
        dest_dir.mkdir(parents=True, exist_ok=True)
    counts = {KIND_IMAGE: 0, KIND_VIDEO: 0, KIND_AUDIO: 0}
    out: list[dict[str, Any]] = []
    for index, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            continue
        kind = _infer_kind(item)
        if kind is None:
            raise UserReferenceError(
                f"reference {index} must be an image, video, or audio file"
            )
        limit = MAX_REFS_BY_KIND[kind]
        if counts[kind] >= limit:
            raise UserReferenceError(
                f"at most {limit} {kind} reference(s) can be attached"
            )
        if preview_only:
            record = _preview_reference(item, kind=kind, index=index)
        else:
            record = _materialize_reference(
                item, kind=kind, dest_dir=dest_dir, index=index
            )
        counts[kind] += 1
        out.append(record)
    return out


def materialize_user_references_for_analysis(raw: Any) -> list[dict[str, Any]]:
    """Decode / copy attachments to a temp dir so classify always gets a path.

    Enter historically used ``dest_dir=None`` preview records. Base64-only
    uploads then had an empty ``path``, so classify/stamp were skipped and the
    classic quality graph regenerated the still. Analysis must materialize.
    """
    if raw in (None, "", []):
        return []
    dest = Path(tempfile.mkdtemp(prefix="jiuwenswarm-designer-refs-"))
    return normalize_user_references(raw, dest_dir=dest)


def image_reference_records(refs: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Image slots that have a non-empty path (ready for classify / stamp)."""
    out: list[dict[str, Any]] = []
    for item in refs or []:
        if not isinstance(item, dict):
            continue
        if str(item.get("kind") or "").strip().lower() != KIND_IMAGE:
            continue
        if str(item.get("path") or "").strip():
            out.append(item)
    return out


def rebase_creative_intent_paths(
    analysis: dict[str, Any] | None,
    refs: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    """Point ``creative_intent.slots`` at project-materialized image paths.

    Classify may have stamped temp paths from
    ``materialize_user_references_for_analysis``; after Enter copies uploads
    into the project ``.designer/refs`` folder, slot paths must follow.
    """
    if not isinstance(analysis, dict):
        return analysis
    intent = analysis.get("creative_intent")
    if not isinstance(intent, dict):
        return analysis
    slots = intent.get("slots")
    if not isinstance(slots, list) or not slots:
        return analysis
    images = image_reference_records(refs)
    if not images:
        return analysis
    for index, slot in enumerate(slots):
        if not isinstance(slot, dict) or index >= len(images):
            continue
        path = str(images[index].get("path") or "").strip()
        if path:
            slot["path"] = path
    return analysis


def _preview_reference(item: dict[str, Any], *, kind: str, index: int) -> dict[str, Any]:
    """In-memory reference slot for analysis (no materialize / mkdir)."""
    path_raw = str(item.get("path") or "").strip()
    uri = str(item.get("uri") or "").strip()
    filename = str(item.get("filename") or Path(path_raw or uri).name or f"{kind}-{index}")
    path = Path(path_raw) if path_raw else Path()
    if not path_raw and uri.startswith("file:"):
        try:
            from urllib.parse import unquote, urlparse
            from urllib.request import url2pathname

            parsed = urlparse(uri)
            path = Path(url2pathname(unquote(parsed.path)))
            path_raw = str(path)
        except Exception:  # noqa: BLE001
            path_raw = ""
    return {
        "id": str(item.get("id") or f"ref_{index:02d}"),
        "kind": kind,
        "role": str(item.get("role") or DEFAULT_ROLE),
        "filename": filename,
        "mime_type": str(item.get("mime_type") or item.get("mimeType") or ""),
        "path": path_raw,
        "uri": uri or (path.resolve().as_uri() if path_raw and path.exists() else ""),
        "size_bytes": int(item.get("size_bytes") or 0),
        "order": index,
        "preview": True,
    }


def _public_record(item: dict[str, Any], index: int) -> dict[str, Any]:
    path = Path(str(item.get("path") or ""))
    uri = str(item.get("uri") or "").strip() or (path.resolve().as_uri() if path.exists() else "")
    record = {
        "id": str(item.get("id") or f"ref_{index:02d}"),
        "kind": str(item.get("kind") or KIND_IMAGE),
        "role": str(item.get("role") or DEFAULT_ROLE),
        "filename": str(item.get("filename") or path.name or f"ref_{index:02d}"),
        "mime_type": str(item.get("mime_type") or ""),
        "path": str(path.resolve()) if str(path) else "",
        "uri": uri,
        "size_bytes": int(item.get("size_bytes") or 0),
        "order": index,
    }
    subject = str(item.get("subject") or "").strip().lower()
    if subject in {"character", "scene", "object"}:
        record["subject"] = subject
    character_id = str(item.get("character_id") or "").strip()
    setting_id = str(item.get("setting_id") or "").strip()
    if character_id:
        record["character_id"] = character_id
    if setting_id:
        record["setting_id"] = setting_id
    return record


def _infer_kind(item: dict[str, Any]) -> str | None:
    explicit = str(item.get("kind") or item.get("type") or "").strip().lower()
    if explicit in SUPPORTED_KINDS:
        return explicit
    mime = str(item.get("mime_type") or item.get("mimeType") or "").strip().lower()
    if mime.startswith("image/"):
        return KIND_IMAGE
    if mime.startswith("video/"):
        return KIND_VIDEO
    if mime.startswith("audio/"):
        return KIND_AUDIO
    name = str(item.get("filename") or item.get("path") or item.get("uri") or "").lower()
    suffix = Path(name.split("?", 1)[0]).suffix
    if suffix in _IMAGE_SUFFIXES:
        return KIND_IMAGE
    if suffix in _VIDEO_SUFFIXES:
        return KIND_VIDEO
    if suffix in _AUDIO_SUFFIXES:
        return KIND_AUDIO
    return None


def _materialize_reference(
    item: dict[str, Any],
    *,
    kind: str,
    dest_dir: Path,
    index: int,
) -> dict[str, Any]:
    filename = str(item.get("filename") or f"{kind}-{index}").strip() or f"{kind}-{index}"
    mime = str(item.get("mime_type") or item.get("mimeType") or "").strip()
    source = _existing_file(
        str(item.get("path") or item.get("uri") or item.get("local_path") or "")
    )
    payload = _decode_inline_bytes(item)
    if source is None and payload is None:
        raise UserReferenceError(f"reference {index} is missing a readable file")
    suffix = Path(filename).suffix.lower()
    if not suffix:
        suffix = _MIME_SUFFIX.get(mime.lower()) or {
            KIND_IMAGE: ".png",
            KIND_VIDEO: ".mp4",
            KIND_AUDIO: ".mp3",
        }[kind]
        filename = f"{filename}{suffix}"
    safe_name = safe_upload_filename(filename, fallback=f"{kind}-{index}{suffix}")
    dest = unique_upload_path(dest_dir / safe_name)
    if payload is not None:
        dest.write_bytes(payload)
    else:
        assert source is not None
        dest.write_bytes(source.read_bytes())
    resolved = dest.resolve()
    return {
        "id": f"ref_{index:02d}",
        "kind": kind,
        "role": str(item.get("role") or DEFAULT_ROLE).strip() or DEFAULT_ROLE,
        "filename": resolved.name,
        "mime_type": mime or _guess_mime(kind, resolved.suffix),
        "path": str(resolved),
        "uri": resolved.as_uri(),
        "size_bytes": resolved.stat().st_size,
        "order": index,
    }


def _existing_file(raw: str) -> Path | None:
    value = (raw or "").strip()
    if not value or value.startswith("blob:") or value.startswith("designer://"):
        return None
    if value.startswith("file:"):
        parsed = urlparse(value)
        path = unquote(parsed.path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        candidate = Path(path)
    else:
        candidate = Path(value)
    try:
        if candidate.is_file():
            return candidate
    except OSError:
        return None
    return None


def _decode_inline_bytes(item: dict[str, Any]) -> bytes | None:
    raw = item.get("base64_data") or item.get("base64Data") or item.get("data")
    if not isinstance(raw, str) or not raw.strip():
        return None
    text = raw.strip()
    match = _DATA_URI_RE.match(text)
    if match:
        text = match.group(2)
    try:
        payload = base64.b64decode(text, validate=False)
    except (binascii.Error, ValueError) as exc:
        raise UserReferenceError("reference payload is not valid base64") from exc
    if len(payload) > MAX_INLINE_BYTES:
        raise UserReferenceError(
            f"inline reference exceeds {MAX_INLINE_BYTES // (1024 * 1024)}MB; "
            "upload a local file path instead"
        )
    return payload or None


def _guess_mime(kind: str, suffix: str) -> str:
    lowered = (suffix or "").lower()
    for mime, mapped in _MIME_SUFFIX.items():
        if mapped == lowered:
            return mime
    return {
        KIND_IMAGE: "image/png",
        KIND_VIDEO: "video/mp4",
        KIND_AUDIO: "audio/mpeg",
    }.get(kind, "application/octet-stream")
