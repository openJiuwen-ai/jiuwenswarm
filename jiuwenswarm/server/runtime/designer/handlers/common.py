# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Shared helpers for Designer node handlers."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import NamedTuple
from urllib.parse import unquote, urlparse

from jiuwenswarm.common.schema.designer_graph import (
    EDGE_KIND_DATA,
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER_DESIGN,
    NODE_ROLE_CLIP,
    NODE_ROLE_SCENE,
    AssetRef,
    DesignerExecutionGraph,
    DesignerExecutionRun,
    DesignerGraphNode,
    edge_kind,
    node_config,
    node_pipeline,
)
from jiuwenswarm.common.utils import get_agent_root_dir, get_agent_workspace_dir
from jiuwenswarm.common.work_mode import DESIGN_WORK_MODE
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.user_references import user_reference_image_paths
from jiuwenswarm.server.runtime.session import project_store

logger = logging.getLogger(__name__)


def graph_prompt(graph: DesignerExecutionGraph, node: DesignerGraphNode | None = None) -> str:
    if node is not None:
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        node_prompt = str(config.get("prompt") or "").strip()
        generate = config.get("generate")
        generate_prompt = ""
        if isinstance(generate, dict):
            generate_prompt = str(generate.get("prompt") or "").strip()
        if node_pipeline(node) == NODE_ROLE_CLIP:
            user = str(graph.get("description") or "")
            try:
                from jiuwenswarm.server.runtime.designer.pipeline.clip_shot_scope import (
                    clip_assignment_text,
                    duration_from_timeline,
                    looks_like_full_story_restatement,
                )
            except Exception:  # noqa: BLE001
                clip_assignment_text = None  # type: ignore[assignment]
                duration_from_timeline = None  # type: ignore[assignment]
                looks_like_full_story_restatement = None  # type: ignore[assignment]

            def _ok(text: str) -> bool:
                if not text:
                    return False
                markers = (
                    "YOUR ASSIGNMENT",
                    "STORYBOARD SHOT",
                    "STAGING LOCK",
                    "SAME-SCENE CONSISTENCY GATE",
                    "Film shot",
                    "CLOTHING LOCK",
                    "LANGUAGE LOCK",
                )
                if any(m in text for m in markers):
                    return True
                if looks_like_full_story_restatement is None:
                    return True
                return not looks_like_full_story_restatement(text, user)

            if _ok(generate_prompt):
                return generate_prompt
            if _ok(node_prompt):
                return node_prompt
            if clip_assignment_text is not None:
                dur = 5
                if duration_from_timeline is not None:
                    dur = duration_from_timeline(str(config.get("timeline") or ""), default=5)
                assignment = clip_assignment_text(
                    shot_index=int(config.get("shot_index") or 1),
                    action=str(config.get("shot_action") or config.get("character_action") or ""),
                    camera=str(config.get("camera") or ""),
                    speech_line=str(config.get("speech_line") or ""),
                    duration_sec=dur,
                    on_screen=list(config.get("on_screen") or config.get("cast_names") or []),
                )
                lock_bits: list[str] = [assignment]
                for key, label in (
                    ("costume_lock", "CLOTHING LOCK"),
                    ("positioning_lock", "STAGING LOCK"),
                    ("language_lock", "LANGUAGE LOCK"),
                    ("speech_line", "SPEECH"),
                ):
                    val = str(config.get(key) or "").strip()
                    if val:
                        lock_bits.append(f"{label}: {val[:400]}")
                return "\n".join(lock_bits)
            return str(config.get("shot_action") or "")[:500] or "this storyboard shot only"
        if node_prompt:
            return node_prompt
        if generate_prompt:
            return generate_prompt
        if node_pipeline(node) == NODE_ROLE_BRIEF:
            pass
    for candidate in graph.get("nodes") or []:
        if node_pipeline(candidate) != NODE_ROLE_BRIEF:
            continue
        brief_config = candidate.get("config") if isinstance(candidate.get("config"), dict) else {}
        brief_prompt = str(brief_config.get("prompt") or "").strip()
        if brief_prompt:
            return brief_prompt
    description = str(graph.get("description") or "").strip()
    if description:
        return description
    title = str(graph.get("title") or "").strip()
    return title or "短视频"


def node_generate_prompt(node: DesignerGraphNode | None) -> str:
    if node is None:
        return ""
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    generate = config.get("generate") if isinstance(config, dict) else None
    if isinstance(generate, dict):
        text = str(generate.get("prompt") or "").strip()
        if text:
            return text
    return str((config or {}).get("prompt") or "").strip()


def path_from_uri(uri: str) -> Path | None:
    value = (uri or "").strip()
    if not value or value.startswith("designer://"):
        return None
    if value.startswith("file:"):
        parsed = urlparse(value)
        path = unquote(parsed.path)
        if len(path) >= 3 and path[0] == "/" and path[2] == ":":
            path = path[1:]
        return Path(path)
    candidate = Path(value)
    return candidate if candidate.exists() else None


def graph_workspace_dir(graph: DesignerExecutionGraph | None = None) -> Path:
    """Return the asset directory owned by a Design project graph."""
    project_id = str(graph.get("project_id") or "").strip() if isinstance(graph, dict) else ""
    if not project_id:
        directory = get_agent_workspace_dir()
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    project = project_store.get_project_by_id(project_id, cache_bust=True)
    if project is None or project.hidden or project.work_mode != DESIGN_WORK_MODE:
        raise ValueError(f"Design project not found for graph: {project_id!r}")

    design_root = (get_agent_root_dir() / "workspace" / DESIGN_WORK_MODE).resolve()
    project_dir = Path(project.project_dir).expanduser().resolve()
    if project_dir.parent != design_root:
        raise ValueError(f"Design project directory is outside the managed root: {project_id!r}")
    directory = project_dir / "assets"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def write_workspace_text(
    stem: str,
    content: str,
    *,
    graph: DesignerExecutionGraph | None = None,
) -> Path:
    directory = graph_workspace_dir(graph)
    path = directory / f"{stem}.md"
    path.write_text(content.strip() + "\n", encoding="utf-8")
    return path.resolve()


def file_output_ref(path: Path, *, kind: str, mime_type: str) -> AssetRef:
    resolved = path.resolve()
    return {
        "kind": kind,
        "uri": resolved.as_uri(),
        "mime_type": mime_type,
        "label": resolved.name,
    }


_IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".jfif"}


def node_output_refs(ctx: NodeExecutionContext, node_id: str) -> list[dict]:
    if ctx.run is None or not node_id:
        return []
    state = (ctx.run.get("node_states") or {}).get(node_id) or {}
    raw_refs = state.get("output_refs")
    collected: list[dict] = []
    if isinstance(raw_refs, list):
        collected = [
            item for item in raw_refs if isinstance(item, dict) and str(item.get("uri") or "").strip()
        ]
    if not collected:
        single = state.get("output_ref") or {}
        if isinstance(single, dict) and str(single.get("uri") or "").strip():
            collected = [single]
    return collected


def _is_image_file(path: Path, ref: dict | None = None) -> bool:
    kind = str((ref or {}).get("kind") or "").lower()
    mime = str((ref or {}).get("mime_type") or "").lower()
    if kind == "image" or mime.startswith("image/"):
        return True
    return path.suffix.lower() in _IMAGE_SUFFIXES


def uploaded_material_image_paths(node: dict | None) -> list[Path]:
    """Image files the user attached on this node, ahead of generated upstream stills."""
    if not isinstance(node, dict):
        return []
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    materials = config.get("materials") if isinstance(config.get("materials"), list) else []
    paths: list[Path] = []
    seen: set[str] = set()
    for item in materials:
        if not isinstance(item, dict):
            continue
        raw = str(item.get("uri") or item.get("path") or "").strip()
        path = path_from_uri(raw)
        if path is None or not path.is_file() or not _is_image_file(path, item):
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def user_replaced_output_image(graph: dict | None, node_id: str) -> list[Path]:
    """The file the user uploaded in place of this node's generated still."""
    if not isinstance(graph, dict) or not node_id:
        return []
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or str(node.get("id") or "") != node_id:
            continue
        config = node.get("config") if isinstance(node.get("config"), dict) else {}
        if not config.get("user_replaced_output"):
            return []
        upload = config.get("upload") if isinstance(config.get("upload"), dict) else {}
        upload_path = path_from_uri(str(upload.get("uri") or ""))
        if (
            upload_path is not None
            and upload_path.is_file()
            and _is_image_file(upload_path, upload)
        ):
            return [upload_path.resolve()]
        ref = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file() or not _is_image_file(path, ref):
            return []
        return [path.resolve()]
    return []


def apply_uploaded_outputs_to_run(run: dict, graph: dict) -> bool:
    """Copy a user-uploaded still into the run so a rerun does not restore the old file."""
    from jiuwenswarm.common.schema.designer_graph import NODE_STATUS_COMPLETED

    if not isinstance(run, dict) or not isinstance(graph, dict):
        return False
    states = run.setdefault("node_states", {})
    if not isinstance(states, dict):
        return False
    changed = False
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        node_id = str(node.get("id") or "")
        replaced = user_replaced_output_image(graph, node_id)
        if not replaced:
            continue
        stored = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
        ref = dict(stored) if isinstance(stored, dict) else {}
        replaced_uri = replaced[0].resolve().as_uri()
        stored_path = path_from_uri(str(ref.get("uri") or ""))
        same_file = (
            stored_path is not None
            and stored_path.is_file()
            and stored_path.resolve() == replaced[0].resolve()
        )
        if not same_file:
            upload = {}
            config = node.get("config") if isinstance(node.get("config"), dict) else {}
            if isinstance(config.get("upload"), dict):
                upload = config["upload"]
            ref = {
                "kind": "image",
                "uri": replaced_uri,
                "mime_type": str(upload.get("mime_type") or ref.get("mime_type") or "image/png"),
            }
        state = dict(states.get(node_id) or {})
        current = state.get("output_ref") if isinstance(state.get("output_ref"), dict) else {}
        if str(current.get("uri") or "") == str(ref.get("uri") or ""):
            continue
        state["status"] = NODE_STATUS_COMPLETED
        state["output_ref"] = dict(ref)
        state["output_refs"] = [dict(ref)]
        state["candidate_output_ref"] = None
        state["candidate_output_refs"] = []
        state["error"] = None
        states[node_id] = state
        changed = True
    return changed


_TEXT_SUFFIXES = {".md", ".txt", ".markdown", ".csv"}
_VIDEO_SUFFIXES = {".mp4", ".mov", ".webm", ".mkv", ".m4v"}
_AUDIO_SUFFIXES = {".mp3", ".wav", ".aac", ".flac", ".ogg", ".m4a"}
_TEXT_KINDS = {"text", "markdown", "storyboard", "brief"}


class PredecessorOutput(NamedTuple):
    """One file or text body that arrived from a predecessor node."""

    node_id: str
    role: str
    label: str
    kind: str
    path: Path | None
    text: str


def _file_kind(path: Path, ref: dict | None = None) -> str:
    kind = str((ref or {}).get("kind") or "").lower()
    mime = str((ref or {}).get("mime_type") or "").lower()
    suffix = path.suffix.lower()
    if kind == "image" or mime.startswith("image/") or suffix in _IMAGE_SUFFIXES:
        return "image"
    if kind == "video" or mime.startswith("video/") or suffix in _VIDEO_SUFFIXES:
        return "video"
    if kind == "audio" or mime.startswith("audio/") or suffix in _AUDIO_SUFFIXES:
        return "audio"
    if kind in _TEXT_KINDS or mime.startswith("text/") or suffix in _TEXT_SUFFIXES:
        return "text"
    return ""


def _graph_node(ctx: NodeExecutionContext | None, node_id: str) -> dict | None:
    graph = ctx.graph if ctx is not None and isinstance(getattr(ctx, "graph", None), dict) else None
    if graph is None:
        return None
    for other in graph.get("nodes") or []:
        if isinstance(other, dict) and str(other.get("id") or "") == node_id:
            return other
    return None


def _locked_replacement_refs(graph: dict | None, node_id: str) -> list[dict] | None:
    """Upload that replaced this node's output.

    None when the node was not replaced. An empty list means the flag is set
    and the file is gone, so the old generated file must not come back.
    """
    node = None
    if isinstance(graph, dict):
        for other in graph.get("nodes") or []:
            if isinstance(other, dict) and str(other.get("id") or "") == node_id:
                node = other
                break
    if not isinstance(node, dict):
        return None
    config = node.get("config") if isinstance(node.get("config"), dict) else {}
    if not config.get("user_replaced_output"):
        return None
    upload = config.get("upload") if isinstance(config.get("upload"), dict) else {}
    upload_path = path_from_uri(str(upload.get("uri") or ""))
    if upload_path is not None and upload_path.is_file():
        return [
            {
                "kind": "",
                "uri": upload_path.resolve().as_uri(),
                "mime_type": str(upload.get("mime_type") or ""),
            }
        ]
    ref = node.get("output_ref") if isinstance(node.get("output_ref"), dict) else {}
    path = path_from_uri(str(ref.get("uri") or ""))
    if path is not None and path.is_file():
        return [dict(ref)]
    return []


def uploaded_output_ref(graph: dict | None, node_id: str) -> dict | None:
    """Run output ref for the media file the user uploaded over this node, if it exists."""
    refs = _locked_replacement_refs(graph, node_id)
    if not refs:
        return None
    ref = dict(refs[0])
    path = path_from_uri(str(ref.get("uri") or ""))
    if path is None:
        return None
    kind = _file_kind(path, ref)
    if kind not in {"image", "video", "audio"}:
        return None
    ref["kind"] = kind
    return ref


def _graph_saved_refs(node: dict | None) -> list[dict]:
    if not isinstance(node, dict):
        return []
    many = node.get("output_refs")
    if isinstance(many, list):
        refs = [
            item
            for item in many
            if isinstance(item, dict) and str(item.get("uri") or "").strip()
        ]
        if refs:
            return refs
    ref = node.get("output_ref")
    if isinstance(ref, dict) and str(ref.get("uri") or "").strip():
        return [ref]
    return []


def _output_refs_for_node(ctx: NodeExecutionContext | None, node_id: str) -> list[dict]:
    """The node's current output: upload replacement, else this run, else the saved graph."""
    if ctx is None or not node_id:
        return []
    graph = ctx.graph if isinstance(getattr(ctx, "graph", None), dict) else None
    locked = _locked_replacement_refs(graph, node_id)
    if locked is not None:
        return locked
    run_refs = node_output_refs(ctx, node_id)
    if run_refs:
        return run_refs
    return _graph_saved_refs(_graph_node(ctx, node_id))


def _text_body(ref: dict) -> str:
    path = path_from_uri(str(ref.get("uri") or ""))
    if path is None:
        return ""
    kind = str(ref.get("kind") or "").lower()
    mime = str(ref.get("mime_type") or "").lower()
    text_like = kind in _TEXT_KINDS or mime.startswith("text/") or path.suffix.lower() in _TEXT_SUFFIXES
    if not text_like:
        return ""
    candidates = [path]
    sidecar = path.with_suffix(".md")
    if sidecar not in candidates:
        candidates.append(sidecar)
    for candidate in candidates:
        if not candidate.is_file():
            continue
        try:
            return candidate.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            continue
    return ""


def node_output_text(ctx: NodeExecutionContext, node_id: str) -> str:
    """Text body of one node's output, when that output is a text file."""
    if ctx is None or not node_id:
        return ""
    for ref in _output_refs_for_node(ctx, node_id):
        body = _text_body(ref).strip()
        if body:
            return body
    return ""


def node_output_video_paths(ctx: NodeExecutionContext, node_id: str) -> list[Path]:
    """Video files produced by one node."""
    if ctx is None or not node_id:
        return []
    paths: list[Path] = []
    seen: set[str] = set()
    for ref in _output_refs_for_node(ctx, node_id):
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file() or _file_kind(path, ref) != "video":
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def node_output_image_paths(ctx: NodeExecutionContext, node_id: str) -> list[Path]:
    """Image files for one node. A user replacement, this run, and the saved graph are one lookup."""
    if ctx is None or not node_id:
        return []
    paths: list[Path] = []
    seen: set[str] = set()
    for ref in _output_refs_for_node(ctx, node_id):
        path = path_from_uri(str(ref.get("uri") or ""))
        if path is None or not path.is_file() or _file_kind(path, ref) != "image":
            continue
        resolved = path.resolve()
        key = str(resolved)
        if key in seen:
            continue
        seen.add(key)
        paths.append(resolved)
    return paths


def predecessor_outputs(
    ctx: NodeExecutionContext | None,
    node: dict | None,
) -> list[PredecessorOutput] | None:
    """Outputs of incoming data-edge sources, in edge order.

    None when this node has no incoming data edges. Each predecessor is resolved
    once, whether its file was uploaded over the output or generated.
    """
    if ctx is None or not isinstance(getattr(ctx, "graph", None), dict) or not isinstance(node, dict):
        return None
    node_id = str(node.get("id") or getattr(ctx, "node_id", "") or "")
    sources: list[str] = []
    wired = False
    for edge in ctx.graph.get("edges") or []:
        if not isinstance(edge, dict) or edge_kind(edge) != EDGE_KIND_DATA:
            continue
        if str(edge.get("target") or "") != node_id:
            continue
        wired = True
        source_id = str(edge.get("source") or "").strip()
        if source_id and source_id not in sources:
            sources.append(source_id)
    if not wired:
        return None
    by_id = {
        str(other.get("id") or ""): other
        for other in (ctx.graph.get("nodes") or [])
        if isinstance(other, dict)
    }
    items: list[PredecessorOutput] = []
    for source_id in sources:
        other = by_id.get(source_id)
        if not isinstance(other, dict):
            continue
        role = node_pipeline(other)
        label = str(other.get("label") or source_id).strip() or source_id
        for ref in _output_refs_for_node(ctx, source_id):
            path = path_from_uri(str(ref.get("uri") or ""))
            if path is None or not path.is_file():
                continue
            kind = _file_kind(path, ref)
            resolved = path.resolve()
            if kind == "text":
                body = _text_body(ref).strip()
                if body:
                    items.append(PredecessorOutput(source_id, role, label, "text", resolved, body))
                continue
            if kind in {"image", "video", "audio"}:
                items.append(PredecessorOutput(source_id, role, label, kind, resolved, ""))
    return items


def role_output_refs(ctx: NodeExecutionContext, role: str) -> list[dict]:
    """Collect output refs from every node with the given role (multi-character/scene)."""
    if ctx.run is None:
        return []
    collected: list[dict] = []
    seen_uri: set[str] = set()
    for node in ctx.graph.get("nodes") or []:
        if node_pipeline(node) != role:
            continue
        for ref in node_output_refs(ctx, str(node.get("id") or "")):
            uri = str(ref.get("uri") or "").strip()
            if not uri or uri in seen_uri:
                continue
            seen_uri.add(uri)
            collected.append(ref)
    return collected


def node_ids_output_image_paths(ctx: NodeExecutionContext, node_ids: list[str]) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    for nid in node_ids:
        for path in node_output_image_paths(ctx, nid):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def collect_frame_reference_images(ctx: NodeExecutionContext, node: dict) -> list[Path]:
    """Continuity refs for keyframes: on_screen solo sheets only (+ optional user refs).

    A file the user attached on this node replaces those generated stills.
    Scene consistency comes from scene_specs + prompt handoff text — not prior KF images.
    Never attach scene specs or off-screen cast sheets.
    """
    attached = uploaded_material_image_paths(node)
    if attached:
        return attached
    cfg = dict(node.get("config") or {})
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    occ = identity.get("occupancy") if isinstance(identity.get("occupancy"), dict) else {}
    on_screen_raw = (
        cfg.get("on_screen")
        or occ.get("must_appear")
        or cfg.get("character_ids")
        or identity.get("character_ids")
        or []
    )
    on_screen = [str(x) for x in on_screen_raw if str(x).strip()]
    preferred = [
        str(x)
        for x in (
            identity.get("character_node_ids")
            or cfg.get("character_node_ids")
            or []
        )
        if str(x).strip()
    ]
    # Prefer preferred list when it already matches on_screen; otherwise resolve solos by cid.
    paths = node_ids_output_image_paths(ctx, preferred) if preferred else []
    if on_screen:
        solo_by_cid: dict[str, str] = {}
        for other in ctx.graph.get("nodes") or []:
            if not isinstance(other, dict):
                continue
            oc = other.get("config") if isinstance(other.get("config"), dict) else {}
            if node_pipeline(other) != NODE_ROLE_CHARACTER_DESIGN:
                continue
            if oc.get("combined_cast"):
                continue
            cids = [str(x) for x in (oc.get("character_ids") or []) if str(x)]
            if len(cids) == 1:
                solo_by_cid[cids[0]] = str(other.get("id") or "")
        on_screen_nodes = [solo_by_cid[c] for c in on_screen if c in solo_by_cid]
        if on_screen_nodes:
            paths = node_ids_output_image_paths(ctx, on_screen_nodes)
    # Fail closed: never fall back to ALL character sheets (cast bleed into wrong KF).
    if not paths and preferred:
        paths = node_ids_output_image_paths(ctx, preferred)

    handoff_id = str(
        identity.get("scene_prompt_handoff_from")
        or cfg.get("scene_prompt_handoff_from")
        or identity.get("scene_master_frame_id")
        or cfg.get("scene_master_frame_id")
        or ""
    ).strip()
    # If master prompt was handed off onto this config, prefer it in the leaf prompt path.
    if handoff_id and not str(cfg.get("scene_master_prompt") or "").strip():
        master_node = next(
            (
                n
                for n in (ctx.graph.get("nodes") or [])
                if isinstance(n, dict) and str(n.get("id") or "") == handoff_id
            ),
            None,
        )
        if master_node:
            mcfg = master_node.get("config") if isinstance(master_node.get("config"), dict) else {}
            mgen = mcfg.get("generate") if isinstance(mcfg.get("generate"), dict) else {}
            master_prompt = str(mgen.get("prompt") or mcfg.get("prompt") or "").strip()
            if master_prompt:
                cfg["scene_master_prompt"] = master_prompt[:2400]
                if isinstance(mcfg.get("scene_specs"), dict):
                    cfg["scene_specs"] = dict(mcfg["scene_specs"])
                node["config"] = cfg

    user_paths = user_reference_image_paths(ctx.graph if isinstance(ctx.graph, dict) else None)

    # Do NOT pull empty NODE_ROLE_Scene specs or prior keyframe images into refs.
    # Continuity is scene_specs + prompt handoff; visual identity is on_screen solos only.
    ordered = [*user_paths, *paths]

    merged: list[Path] = []
    seen: set[str] = set()
    for path in ordered:
        if path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}:
            continue
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        merged.append(path)
    # Cap refs — DashScope often rejects large multi-ref batches.
    return merged[:4]


def role_output_image_paths(ctx: NodeExecutionContext, role: str) -> list[Path]:
    paths: list[Path] = []
    seen: set[str] = set()
    graph = ctx.graph if isinstance(getattr(ctx, "graph", None), dict) else {}
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict) or node_pipeline(node) != role:
            continue
        for path in node_output_image_paths(ctx, str(node.get("id") or "")):
            key = str(path.resolve())
            if key in seen:
                continue
            seen.add(key)
            paths.append(path)
    return paths


def role_output_image_path(ctx: NodeExecutionContext, role: str) -> Path | None:
    paths = role_output_image_paths(ctx, role)
    return paths[0] if paths else None


def text_output_path(ref: AssetRef | None) -> Path | None:
    """Resolve a local text artifact, including an existing media sidecar."""
    path = path_from_uri(str((ref or {}).get("uri") or ""))
    if path is None:
        return None
    candidates = [path] if path.suffix.lower() in {".md", ".txt", ".markdown", ".csv"} else []
    candidates.append(path.with_suffix(".md"))
    return next((candidate for candidate in candidates if candidate.is_file()), None)


def read_node_text(
    node: DesignerGraphNode, run: DesignerExecutionRun | None = None
) -> tuple[str, Path | None]:
    """Read accepted run output, graph output, then executable draft; never candidates."""
    state = ((run or {}).get("node_states") or {}).get(node["id"]) or {}
    refs = [state.get("output_ref"), *(state.get("output_refs") or []), node.get("output_ref")]
    for ref in refs:
        path = text_output_path(ref)
        if path is not None:
            with path.open(encoding="utf-8", newline="") as source:
                return source.read(), path
    cfg = node_config(node)
    return str(cfg.get("prewritten") or cfg.get("draft_prewritten") or ""), None


def role_output_text(ctx: NodeExecutionContext, role: str) -> str:
    for node in ctx.graph.get("nodes") or []:
        if node_pipeline(node) == role:
            text, path = read_node_text(node, ctx.run)
            if path is not None or text:
                return text
    return ""


async def complete_designer_text(prompt: str, *, max_tokens: int = 8192) -> str:
    """Call the default chat model. Tests monkeypatch this function."""
    from jiuwenswarm.common.config import get_config, get_default_models
    from openjiuwen.core.foundation.llm import Model
    from openjiuwen.core.foundation.llm.schema.config import (
        ModelClientConfig,
        ModelRequestConfig,
    )

    entries = get_default_models(get_config())
    entry = next((item for item in entries if item.get("is_default") is True), None)
    if entry is None and entries:
        entry = entries[0]
    client = (entry or {}).get("model_client_config") if isinstance(entry, dict) else {}
    mco = (entry or {}).get("model_config_obj") if isinstance(entry, dict) else {}
    if not isinstance(client, dict):
        return ""
    if not isinstance(mco, dict):
        mco = {}
    api_key = str(client.get("api_key") or "").strip()
    api_base = str(client.get("api_base") or "").strip()
    model_name = str(client.get("model_name") or "").strip()
    provider = str(client.get("client_provider") or "").strip()
    if not api_key or not model_name:
        return ""
    kwargs: dict[str, object] = {
        "api_key": api_key,
        "api_base": api_base,
        "client_provider": provider,
    }
    profile = str(client.get("endpoint_profile") or "").strip()
    if profile:
        kwargs["endpoint_profile"] = profile
    request = ModelRequestConfig(
        model_name=model_name,
        temperature=float(mco.get("temperature", 0.4) or 0.4),
        top_p=float(mco.get("top_p", 0.95) or 0.95),
        max_tokens=max_tokens,
    )
    model = Model(
        model_client_config=ModelClientConfig(**kwargs),
        model_config=request,
    )
    invoke_input = {
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.4,
        "max_tokens": max_tokens,
        "model": model_name,
    }
    from jiuwenswarm.server.runtime.designer.trajectory import (
        current_trajectory_span,
    )

    with current_trajectory_span(
        action="agent_call",
        phase="inference",
        detail={
            "agent_type": "chat_model",
            "prompt": prompt,
            "system_prompt": "",
            "input": invoke_input,
        },
    ) as span_payload:
        response = await model.invoke(**invoke_input)
        content = getattr(response, "content", response)
        span_payload["output"] = content
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if isinstance(block, dict) and block.get("text"):
                parts.append(str(block["text"]))
            elif isinstance(block, str):
                parts.append(block)
        return "\n".join(parts).strip()
    return str(content).strip()


_IMAGE_GEN_SEM = None


def _image_gen_semaphore():
    """Limit concurrent image calls to avoid provider RateQuota 429s."""
    import asyncio

    global _IMAGE_GEN_SEM
    if _IMAGE_GEN_SEM is None:
        # Keep low: quality path fans out cast+scene+frames; API quotas are tight.
        _IMAGE_GEN_SEM = asyncio.Semaphore(2)
    return _IMAGE_GEN_SEM


def _is_rate_limit_error(text: str) -> bool:
    low = (text or "").lower()
    return any(
        token in low
        for token in (
            "ratequota",
            "rate limit",
            "throttling",
            "too many requests",
            "429",
        )
    )


async def generate_designer_image(
    prompt: str,
    size: str = "1K",
    reference_image: str | None = None,
    reference_images: list[str] | None = None,
    max_tries: int = 2,
    timeout_sec: float = 1200.0,
) -> dict[str, str] | None:
    """Call the Settings > Agent image model when enabled. Tests monkeypatch this function.

    Retries transient RateQuota/429 with backoff under a global concurrency cap.
    """
    import asyncio

    from jiuwenswarm.common.utils import get_env_file
    from jiuwenswarm.dotenv_early import load_dotenv_runtime
    from jiuwenswarm.server.runtime.designer import media_generation

    try:
        load_dotenv_runtime(dotenv_path=get_env_file(), override=True)
    except Exception:
        logger.debug("Failed to reload image generation env before generation", exc_info=True)

    if not media_generation.generation_enabled("image"):
        logger.info("Designer image generation skipped: VISUAL_GEN_ENABLED is off")
        return None
    refs = [str(item).strip() for item in (reference_images or []) if str(item).strip()]
    single = (reference_image or "").strip()
    if single and single not in refs:
        refs.insert(0, single)
    if refs:
        logger.info(
            "Designer image generation using reference_images=%s",
            ",".join(Path(item).name for item in refs),
        )

    attempts = max(1, min(6, int(max_tries or 1)))
    last_error = ""
    sem = _image_gen_semaphore()
    for attempt in range(1, attempts + 1):
        async with sem:
            try:
                tool_input = {
                    "prompt": prompt,
                    "size": size,
                    "reference_images": refs or None,
                }
                from jiuwenswarm.server.runtime.designer.trajectory import (
                    current_trajectory_span,
                )

                with current_trajectory_span(
                    action="tool_call",
                    tool="image_generation",
                    phase="tool",
                    detail={"attempt": attempt, "input": tool_input},
                ):
                    result = await asyncio.wait_for(
                        media_generation.generate_image(
                            prompt,
                            size=size,
                            reference_images=refs or None,
                        ),
                        timeout=max(60.0, float(timeout_sec or 1200.0)),
                    )
            except asyncio.TimeoutError:
                last_error = f"image generation timed out after {int(timeout_sec or 1200)}s"
                logger.info("Designer image generation timed out (attempt %s/%s)", attempt, attempts)
                result = {"error": last_error}
            except Exception as exc:  # noqa: BLE001
                last_error = str(exc)
                result = {"error": last_error}

        if isinstance(result, dict) and result.get("image_path"):
            return {"image_path": str(result["image_path"])}
        err = str((result or {}).get("error") or last_error or "image generation failed")
        last_error = err
        if attempt < attempts and _is_rate_limit_error(err):
            delay = min(45.0, 4.0 * (2 ** (attempt - 1)))
            logger.info(
                "Designer image rate-limited; retry in %.1fs (attempt %s/%s)",
                delay,
                attempt,
                attempts,
            )
            await asyncio.sleep(delay)
            continue
        if attempt < attempts and "timed out" in err.lower():
            if attempt >= 2:
                break
            await asyncio.sleep(2.0)
            continue
        break

    logger.info("Designer image generation unavailable: %s", last_error)
    return {"error": last_error}
