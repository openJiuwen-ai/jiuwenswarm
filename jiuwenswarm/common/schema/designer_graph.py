# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer execution graph domain schema (canonical Python contract).

Shared between the React Flow frontend, gateway RPC handlers, and the graph
executor.  Frontend mirrors live in
``channels/web/frontend/src/features/designer/executionGraphTypes.ts``;
cross-layer literals are pinned by
``tests/unit_tests/test_designer_execution_graph_contract.py``.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, TypedDict

# ── Schema version ────────────────────────────────────────────────────────────

SCHEMA_VERSION = "designer-execution-graph.v1"
"""Domain graph payload version."""

RUN_SCHEMA_VERSION = "designer-execution-run.v1"
"""Execution run state payload version."""

# ── Node types (modality, contract-pinned) ────────────────────────────────────

NODE_TYPE_TEXT = "text"
NODE_TYPE_TABLE = "table"
NODE_TYPE_IMAGE = "image"
NODE_TYPE_VIDEO = "video"
NODE_TYPE_AUDIO = "audio"

NODE_TYPES: frozenset[str] = frozenset(
    {
        NODE_TYPE_TEXT,
        NODE_TYPE_TABLE,
        NODE_TYPE_IMAGE,
        NODE_TYPE_VIDEO,
        NODE_TYPE_AUDIO,
    }
)

# ── Node roles (canvas modality; same strings as node.type, MiniMax-style) ────

NODE_ROLE_TEXT = NODE_TYPE_TEXT
NODE_ROLE_TABLE = NODE_TYPE_TABLE
NODE_ROLE_IMAGE = NODE_TYPE_IMAGE
NODE_ROLE_VIDEO = NODE_TYPE_VIDEO
NODE_ROLE_AUDIO = NODE_TYPE_AUDIO

NODE_ROLES: frozenset[str] = frozenset(NODE_TYPES)

# Internal generation recipes. Not a canvas node kind — MiniMax keeps Character /
# Storyboard / Clip in Plan docs and filenames, not in node.type.
PIPELINE_BRIEF = "brief"
PIPELINE_CHARACTER_DESIGN = "character_design"
PIPELINE_SCENE = "scene"
PIPELINE_STORYBOARD = "storyboard"
PIPELINE_FRAME = "frame"
PIPELINE_CLIP = "clip"
PIPELINE_COMPOSE = "compose"
PIPELINE_MUSIC = "music"
PIPELINE_SPEECH = "speech"

# Back-compat aliases used by handlers / tests for pipeline identity.
NODE_ROLE_BRIEF = PIPELINE_BRIEF
NODE_ROLE_CHARACTER_DESIGN = PIPELINE_CHARACTER_DESIGN
NODE_ROLE_SCENE = PIPELINE_SCENE
NODE_ROLE_STORYBOARD = PIPELINE_STORYBOARD
NODE_ROLE_FRAME = PIPELINE_FRAME
NODE_ROLE_CLIP = PIPELINE_CLIP
NODE_ROLE_COMPOSE = PIPELINE_COMPOSE
NODE_ROLE_MUSIC = PIPELINE_MUSIC
NODE_ROLE_SPEECH = PIPELINE_SPEECH

PIPELINES: frozenset[str] = frozenset(
    {
        PIPELINE_BRIEF,
        PIPELINE_CHARACTER_DESIGN,
        PIPELINE_SCENE,
        PIPELINE_STORYBOARD,
        PIPELINE_FRAME,
        PIPELINE_CLIP,
        PIPELINE_COMPOSE,
        PIPELINE_MUSIC,
        PIPELINE_SPEECH,
    }
)

PIPELINE_TO_TYPE: dict[str, str] = {
    PIPELINE_BRIEF: NODE_TYPE_TEXT,
    PIPELINE_CHARACTER_DESIGN: NODE_TYPE_IMAGE,
    PIPELINE_SCENE: NODE_TYPE_IMAGE,
    PIPELINE_STORYBOARD: NODE_TYPE_TABLE,
    PIPELINE_FRAME: NODE_TYPE_IMAGE,
    PIPELINE_CLIP: NODE_TYPE_VIDEO,
    PIPELINE_COMPOSE: NODE_TYPE_VIDEO,
    PIPELINE_MUSIC: NODE_TYPE_AUDIO,
    PIPELINE_SPEECH: NODE_TYPE_AUDIO,
}

_MODALITY_TITLES = {
    NODE_TYPE_TEXT: "Text",
    NODE_TYPE_TABLE: "Table",
    NODE_TYPE_IMAGE: "Image",
    NODE_TYPE_VIDEO: "Video",
    NODE_TYPE_AUDIO: "Audio",
}

_LEGACY_PIPELINE_TITLES = frozenset(
    {
        "项目 brief",
        "Brief",
        "brief",
        "角色图",
        "Character",
        "character",
        "场景图",
        "场景",
        "Scene",
        "scene",
        "分镜表",
        "Storyboard",
        "storyboard",
        "成片",
        "Film",
        "Compose",
        "compose",
        "Speech / TTS",
        "Music / Bed",
        "最终视频",
    }
)
_INDEXED_LABEL_RE = re.compile(
    r"^(?:关键帧|视频片段|Keyframe|Clip)(?:\s*(\d+))?(?:首帧)?$",
    re.IGNORECASE,
)


def modality_node_label(node_type: str, index: int = 0) -> str:
    """Canvas title from modality. MiniMax nodes are Image / Video / Audio / Text."""
    base = _MODALITY_TITLES.get(node_type, "")
    if not base:
        return ""
    if index and index > 0:
        return f"{base} {index}"
    return base


def pipeline_node_label(role: str, shot_index: int = 1) -> str:
    """Canvas title for a role or pipeline id — always a modality name."""
    del shot_index
    modality = PIPELINE_TO_TYPE.get(role, role)
    return modality_node_label(modality)


def english_pipeline_label(label: str, role: str, shot_index: int = 1) -> str:
    """Rewrite known pipeline titles to modality names; leave semantic / custom names alone."""
    text = (label or "").strip()
    if not text:
        desired = pipeline_node_label(role, shot_index)
        return desired or text
    # Designer semantic scheme (Brief: … / Character N: … / Scene N: Shot …) must survive.
    try:
        from jiuwenswarm.server.runtime.designer.node_labels import is_semantic_canvas_label

        if is_semantic_canvas_label(text):
            return text
    except Exception:  # noqa: BLE001
        pass
    # Any label with a descriptive suffix after ":" is author/LLM-owned.
    if ":" in text and not _INDEXED_LABEL_RE.match(text):
        return text
    desired = pipeline_node_label(role, shot_index)
    if not desired:
        return text
    if text in _LEGACY_PIPELINE_TITLES or text.casefold() in {
        item.casefold() for item in _LEGACY_PIPELINE_TITLES
    }:
        return desired
    indexed = _INDEXED_LABEL_RE.match(text)
    if indexed:
        return desired
    return text


def infer_pipeline_from_id(node_id: str) -> str:
    """Bootstrap / static graph ids still encode the generation recipe."""
    value = str(node_id or "").strip()
    if value == "n_brief":
        return PIPELINE_BRIEF
    if value == "n_character" or value.startswith("n_character_"):
        return PIPELINE_CHARACTER_DESIGN
    if value == "n_scene" or value.startswith("n_scene_"):
        return PIPELINE_SCENE
    if value == "n_storyboard":
        return PIPELINE_STORYBOARD
    if value in {"n_compose", "n_final"}:
        return PIPELINE_COMPOSE
    if value == "n_speech":
        return PIPELINE_SPEECH
    if value == "n_music":
        return PIPELINE_MUSIC
    if value == "n_frame" or value.startswith("n_frame_"):
        return PIPELINE_FRAME
    if value == "n_clip" or value.startswith("n_clip_"):
        return PIPELINE_CLIP
    return ""

# ── Node config (role-discriminated; modality stays on node.type) ─────────────

CONFIG_KEY_ROLE = "role"
CONFIG_KEY_PIPELINE = "pipeline"
CONFIG_KEY_PROMPT = "prompt"
CONFIG_KEY_INPUTS = "inputs"
CONFIG_KEY_DELEGATE = "delegate"
CONFIG_KEY_AGENT_TEMPLATE = "agent_template"
CONFIG_KEY_COLLABORATE = "collaborate"
CONFIG_KEY_GENERATE = "generate"
CONFIG_KEY_UPLOAD = "upload"
CONFIG_KEY_EDIT = "edit"
CONFIG_KEY_INTERACTION_MODE = "interaction_mode"
CONFIG_KEY_MATERIALS = "materials"

CONFIG_KEYS: frozenset[str] = frozenset(
    {
        CONFIG_KEY_ROLE,
        CONFIG_KEY_PIPELINE,
        CONFIG_KEY_PROMPT,
        CONFIG_KEY_INPUTS,
        CONFIG_KEY_DELEGATE,
        CONFIG_KEY_AGENT_TEMPLATE,
        CONFIG_KEY_COLLABORATE,
        CONFIG_KEY_GENERATE,
        CONFIG_KEY_UPLOAD,
        CONFIG_KEY_EDIT,
        CONFIG_KEY_INTERACTION_MODE,
        CONFIG_KEY_MATERIALS,
    }
)

CONFIG_INTERACTION_MODES: frozenset[str] = frozenset({"generate", "upload", "edit"})

GENERATE_PROMPT_ORIGIN_STORYBOARD = "storyboard"
GENERATE_PROMPT_ORIGIN_USER = "user"

CONFIG_DELEGATE_HANDLER = "handler"
CONFIG_DELEGATE_SUBAGENT = "subagent"
CONFIG_DELEGATE_AGENT = "agent"

CONFIG_DELEGATES: frozenset[str] = frozenset(
    {
        CONFIG_DELEGATE_HANDLER,
        CONFIG_DELEGATE_SUBAGENT,
        CONFIG_DELEGATE_AGENT,
    }
)

DESIGNER_AGENT_GROUP_NAME = "designer"

ROLE_DEFAULT_TEMPLATES: dict[str, str] = {
    PIPELINE_BRIEF: f"{DESIGNER_AGENT_GROUP_NAME}/leader",
    PIPELINE_CHARACTER_DESIGN: f"{DESIGNER_AGENT_GROUP_NAME}/character",
    PIPELINE_SCENE: f"{DESIGNER_AGENT_GROUP_NAME}/scene",
    PIPELINE_STORYBOARD: f"{DESIGNER_AGENT_GROUP_NAME}/storyboard",
    PIPELINE_FRAME: f"{DESIGNER_AGENT_GROUP_NAME}/frame",
    PIPELINE_CLIP: f"{DESIGNER_AGENT_GROUP_NAME}/clip",
    PIPELINE_COMPOSE: f"{DESIGNER_AGENT_GROUP_NAME}/clip",
    NODE_TYPE_TEXT: f"{DESIGNER_AGENT_GROUP_NAME}/leader",
    NODE_TYPE_TABLE: f"{DESIGNER_AGENT_GROUP_NAME}/storyboard",
    NODE_TYPE_IMAGE: f"{DESIGNER_AGENT_GROUP_NAME}/character",
    NODE_TYPE_VIDEO: f"{DESIGNER_AGENT_GROUP_NAME}/clip",
}

# ── Edge kinds ────────────────────────────────────────────────────────────────

EDGE_KIND_DATA = "data"
EDGE_KIND_SYNC = "sync"

EDGE_KINDS: frozenset[str] = frozenset({EDGE_KIND_DATA, EDGE_KIND_SYNC})

# ── Graph sources ─────────────────────────────────────────────────────────────

GRAPH_SOURCE_PROMPT = "prompt"
GRAPH_SOURCE_MANUAL = "manual"

GRAPH_SOURCES: frozenset[str] = frozenset(
    {
        GRAPH_SOURCE_PROMPT,
        GRAPH_SOURCE_MANUAL,
    }
)

# ── Node / run statuses ───────────────────────────────────────────────────────

NODE_STATUS_PENDING = "pending"
NODE_STATUS_RUNNING = "running"
NODE_STATUS_COMPLETED = "completed"
NODE_STATUS_FAILED = "failed"
NODE_STATUS_CANCELLED = "cancelled"

# Virtual node: invisible leader activity lives in run.node_states, never on the canvas.
LEADER_NODE_ID = "__leader__"

ACTIVITY_KIND_THINKING = "thinking"
ACTIVITY_KIND_TOOL_CALL = "tool_call"
ACTIVITY_KIND_STAGE = "stage"
ACTIVITY_KINDS: frozenset[str] = frozenset(
    {
        ACTIVITY_KIND_THINKING,
        ACTIVITY_KIND_TOOL_CALL,
        ACTIVITY_KIND_STAGE,
    }
)
ACTIVITY_TEXT_MAX = 120
ACTIVITY_TAIL_LIMIT = 8
ACTIVITY_LOG_LIMIT = 20

NODE_STATUSES: frozenset[str] = frozenset(
    {
        NODE_STATUS_PENDING,
        NODE_STATUS_RUNNING,
        NODE_STATUS_COMPLETED,
        NODE_STATUS_FAILED,
        NODE_STATUS_CANCELLED,
    }
)

RUN_STATUS_DRAFT = "draft"
RUN_STATUS_RUNNING = "running"
RUN_STATUS_PAUSED = "paused"
RUN_STATUS_COMPLETED = "completed"
RUN_STATUS_FAILED = "failed"
RUN_STATUS_CANCELLED = "cancelled"

RUN_STATUSES: frozenset[str] = frozenset(
    {
        RUN_STATUS_DRAFT,
        RUN_STATUS_RUNNING,
        RUN_STATUS_PAUSED,
        RUN_STATUS_COMPLETED,
        RUN_STATUS_FAILED,
        RUN_STATUS_CANCELLED,
    }
)

# ── ID helpers ────────────────────────────────────────────────────────────────

_GRAPH_ID_PREFIX = "graph_"
_RUN_ID_PREFIX = "run_"
_ID_HEX_LEN = 8


def new_graph_id() -> str:
    return f"{_GRAPH_ID_PREFIX}{secrets.token_hex(_ID_HEX_LEN)}"


def new_run_id() -> str:
    return f"{_RUN_ID_PREFIX}{secrets.token_hex(_ID_HEX_LEN)}"


def utc_now_ms() -> int:
    return int(time.time() * 1000)


# ── Typed payloads ──────────────────────────────────────────────────────────────


class AssetRef(TypedDict, total=False):
    kind: str
    uri: str
    mime_type: str
    label: str


class NodeLayout(TypedDict, total=False):
    x: float
    y: float
    width: float
    height: float


class DesignerNodeConfig(TypedDict, total=False):
    """Typed node config. ``role`` is the canvas modality; ``pipeline`` is internal."""

    role: str
    pipeline: str
    prompt: str
    inputs: list[str]
    delegate: str
    agent_template: str
    collaborate: bool
    generate: dict[str, Any]
    upload: dict[str, Any]
    edit: dict[str, Any]
    interaction_mode: str
    materials: list[Any]
    shot_index: int


class DesignerGraphPatch(TypedDict, total=False):
    title: str
    description: str
    upsert_nodes: list[Any]
    upsert_edges: list[Any]
    remove_node_ids: list[str]
    remove_edge_ids: list[str]


class DesignerGraphNode(TypedDict, total=False):
    id: str
    type: str
    label: str
    config: DesignerNodeConfig
    layout: NodeLayout
    output_ref: AssetRef | None


class DesignerGraphEdge(TypedDict, total=False):
    id: str
    source: str
    target: str
    kind: str
    label: str


class DesignerExecutionGraph(TypedDict, total=False):
    schema_version: str
    graph_id: str
    project_id: str
    title: str
    description: str
    source: str
    nodes: list[DesignerGraphNode]
    edges: list[DesignerGraphEdge]
    metadata: dict[str, Any]
    created_at: int
    updated_at: int


class DesignerNodeActivity(TypedDict, total=False):
    kind: str
    text: str
    tool: str
    at: int


class DesignerNodeState(TypedDict, total=False):
    status: str
    started_at: int | None
    completed_at: int | None
    output_ref: AssetRef | None
    output_refs: list[AssetRef]
    candidate_output_ref: AssetRef | None
    candidate_output_refs: list[AssetRef]
    error: str | None
    blocked_by: list[str]
    activity: DesignerNodeActivity
    activity_tail: list[str]
    activity_log: list[DesignerNodeActivity]


class DesignerExecutionRun(TypedDict, total=False):
    schema_version: str
    run_id: str
    graph_id: str
    project_id: str
    status: str
    node_states: dict[str, DesignerNodeState]
    current_node_ids: list[str]
    created_at: int
    updated_at: int
    metadata: dict[str, Any]
    # User-visible failure message for async run failures (e.g. DesignerLlmError).
    error: str | None
    warning: str | None
    warnings: list[str]


# ── Validation / normalization ────────────────────────────────────────────────


class DesignerGraphValidationError(ValueError):
    """Raised when an execution graph payload fails schema validation."""


def _require_str(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesignerGraphValidationError(f"{field} must be a non-empty string")
    return value.strip()


def normalize_layout(raw: Any) -> NodeLayout:
    layout: NodeLayout = {}
    if not isinstance(raw, dict):
        return layout
    for key in ("x", "y", "width", "height"):
        val = raw.get(key)
        if isinstance(val, (int, float)) and not isinstance(val, bool):
            layout[key] = float(val)  # type: ignore[literal-required]
    return layout


def normalize_asset_ref(raw: Any) -> AssetRef | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("output_ref must be an object")
    kind = raw.get("kind")
    uri = raw.get("uri")
    if not isinstance(kind, str) or not kind.strip():
        raise DesignerGraphValidationError("output_ref.kind must be a non-empty string")
    if not isinstance(uri, str) or not uri.strip():
        raise DesignerGraphValidationError("output_ref.uri must be a non-empty string")
    ref: AssetRef = {"kind": kind.strip(), "uri": uri.strip()}
    mime_type = raw.get("mime_type")
    if isinstance(mime_type, str) and mime_type.strip():
        ref["mime_type"] = mime_type.strip()
    label = raw.get("label")
    if isinstance(label, str) and label.strip():
        ref["label"] = label.strip()
    return ref


def normalize_node_config(raw: Any) -> DesignerNodeConfig:
    """Validate role-discriminated node config; keep unknown keys for forward compat."""
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node.config must be an object")
    config: dict[str, Any] = {
        key: value for key, value in raw.items() if key not in CONFIG_KEYS
    }
    pipeline = raw.get(CONFIG_KEY_PIPELINE)
    if pipeline is not None and not (isinstance(pipeline, str) and not pipeline.strip()):
        if not isinstance(pipeline, str) or not pipeline.strip():
            raise DesignerGraphValidationError("node.config.pipeline must be a non-empty string")
        pipeline = pipeline.strip()
        if pipeline not in PIPELINES:
            raise DesignerGraphValidationError(f"unsupported node pipeline: {pipeline!r}")
        config[CONFIG_KEY_PIPELINE] = pipeline
    role = raw.get(CONFIG_KEY_ROLE)
    if role is not None and not (isinstance(role, str) and not role.strip()):
        if not isinstance(role, str) or not role.strip():
            raise DesignerGraphValidationError("node.config.role must be a non-empty string")
        role = role.strip()
        if role in PIPELINES:
            if CONFIG_KEY_PIPELINE not in config:
                config[CONFIG_KEY_PIPELINE] = role
            role = PIPELINE_TO_TYPE[role]
        if role not in NODE_ROLES:
            raise DesignerGraphValidationError(f"unsupported node role: {role!r}")
        config[CONFIG_KEY_ROLE] = role
    prompt = raw.get(CONFIG_KEY_PROMPT)
    if prompt is not None:
        if not isinstance(prompt, str):
            raise DesignerGraphValidationError("node.config.prompt must be a string")
        config[CONFIG_KEY_PROMPT] = prompt
    inputs = raw.get(CONFIG_KEY_INPUTS)
    if inputs is not None:
        if not isinstance(inputs, list) or not all(
            isinstance(item, str) and item.strip() for item in inputs
        ):
            raise DesignerGraphValidationError(
                "node.config.inputs must be a non-empty-string array"
            )
        config[CONFIG_KEY_INPUTS] = [str(item).strip() for item in inputs]
    delegate = raw.get(CONFIG_KEY_DELEGATE)
    if delegate is not None and not (isinstance(delegate, str) and not delegate.strip()):
        if not isinstance(delegate, str) or delegate.strip() not in CONFIG_DELEGATES:
            raise DesignerGraphValidationError(
                "node.config.delegate must be 'handler', 'agent', or 'subagent'"
            )
        config[CONFIG_KEY_DELEGATE] = delegate.strip()
    agent_template = raw.get(CONFIG_KEY_AGENT_TEMPLATE)
    if agent_template is not None and not (
        isinstance(agent_template, str) and not agent_template.strip()
    ):
        if not isinstance(agent_template, str) or not agent_template.strip():
            raise DesignerGraphValidationError(
                "node.config.agent_template must be a non-empty string"
            )
        config[CONFIG_KEY_AGENT_TEMPLATE] = agent_template.strip()
    collaborate = raw.get(CONFIG_KEY_COLLABORATE)
    if collaborate is not None:
        if not isinstance(collaborate, bool):
            raise DesignerGraphValidationError("node.config.collaborate must be a boolean")
        config[CONFIG_KEY_COLLABORATE] = collaborate
    for object_key in (CONFIG_KEY_GENERATE, CONFIG_KEY_UPLOAD, CONFIG_KEY_EDIT):
        value = raw.get(object_key)
        if value is None:
            continue
        if not isinstance(value, dict):
            raise DesignerGraphValidationError(f"node.config.{object_key} must be an object")
        config[object_key] = dict(value)
    interaction_mode = raw.get(CONFIG_KEY_INTERACTION_MODE)
    if interaction_mode is not None:
        if (
            not isinstance(interaction_mode, str)
            or interaction_mode.strip() not in CONFIG_INTERACTION_MODES
        ):
            raise DesignerGraphValidationError(
                "node.config.interaction_mode must be generate, upload, or edit"
            )
        config[CONFIG_KEY_INTERACTION_MODE] = interaction_mode.strip()
    materials = raw.get(CONFIG_KEY_MATERIALS)
    if materials is not None:
        if not isinstance(materials, list):
            raise DesignerGraphValidationError("node.config.materials must be an array")
        config[CONFIG_KEY_MATERIALS] = list(materials)
    return config  # type: ignore[return-value]


def normalize_node(raw: Any) -> DesignerGraphNode:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node must be an object")
    node_id = _require_str(raw.get("id"), "node.id")
    node_type = _require_str(raw.get("type"), "node.type")
    if node_type not in NODE_TYPES:
        raise DesignerGraphValidationError(f"unsupported node type: {node_type!r}")
    label = raw.get("label")
    config = normalize_node_config(raw.get("config"))
    if not str(config.get(CONFIG_KEY_PIPELINE) or "").strip():
        inferred = infer_pipeline_from_id(node_id)
        if inferred:
            config[CONFIG_KEY_PIPELINE] = inferred
    if not str(config.get(CONFIG_KEY_ROLE) or "").strip():
        config[CONFIG_KEY_ROLE] = node_type
    role = str(config.get(CONFIG_KEY_ROLE) or node_type).strip()
    shot_raw = config.get("shot_index")
    try:
        shot_index = int(shot_raw) if shot_raw is not None else 1
    except (TypeError, ValueError):
        shot_index = 1
    current_label = str(label).strip() if isinstance(label, str) and label.strip() else node_id
    node: DesignerGraphNode = {
        "id": node_id,
        "type": node_type,
        "label": english_pipeline_label(current_label, role, shot_index),
        "config": config,
        "layout": normalize_layout(raw.get("layout")),
    }
    if "output_ref" in raw:
        node["output_ref"] = normalize_asset_ref(raw.get("output_ref"))
    return node


def normalize_edge(raw: Any) -> DesignerGraphEdge:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("edge must be an object")
    edge_id = _require_str(raw.get("id"), "edge.id")
    source = _require_str(raw.get("source"), "edge.source")
    target = _require_str(raw.get("target"), "edge.target")
    kind_raw = raw.get("kind")
    kind = EDGE_KIND_DATA
    if isinstance(kind_raw, str) and kind_raw.strip():
        kind = kind_raw.strip()
    if kind not in EDGE_KINDS:
        raise DesignerGraphValidationError(f"unsupported edge kind: {kind!r}")
    edge: DesignerGraphEdge = {
        "id": edge_id,
        "source": source,
        "target": target,
        "kind": kind,
    }
    label = raw.get("label")
    if isinstance(label, str) and label.strip():
        text = label.strip()
        edge["label"] = "Align" if text in {"对齐", "Align", "align"} else text
    return edge


def normalize_execution_graph(raw: Any) -> DesignerExecutionGraph:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("graph must be an object")
    schema_version = _require_str(raw.get("schema_version"), "schema_version")
    if schema_version != SCHEMA_VERSION:
        raise DesignerGraphValidationError(
            f"unsupported schema_version: {schema_version!r} (expected {SCHEMA_VERSION!r})"
        )
    graph_id = _require_str(raw.get("graph_id"), "graph_id")
    project_id = _require_str(raw.get("project_id"), "project_id")
    title = raw.get("title")
    description = raw.get("description")
    source = raw.get("source")
    graph_source = GRAPH_SOURCE_MANUAL
    if isinstance(source, str) and source.strip():
        graph_source = source.strip()
        if graph_source not in GRAPH_SOURCES:
            raise DesignerGraphValidationError(f"unsupported source: {graph_source!r}")
    raw_nodes = raw.get("nodes")
    if not isinstance(raw_nodes, list):
        raise DesignerGraphValidationError("nodes must be an array")
    raw_edges = raw.get("edges")
    if not isinstance(raw_edges, list):
        raise DesignerGraphValidationError("edges must be an array")
    nodes = [normalize_node(item) for item in raw_nodes]
    edges = [normalize_edge(item) for item in raw_edges]
    node_ids = {node["id"] for node in nodes}
    if len(node_ids) != len(nodes):
        raise DesignerGraphValidationError("node ids must be unique")
    for edge in edges:
        if edge["source"] not in node_ids or edge["target"] not in node_ids:
            raise DesignerGraphValidationError(
                f"edge {edge['id']!r} references unknown node id"
            )
    metadata = raw.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        raise DesignerGraphValidationError("metadata must be an object")
    now = utc_now_ms()
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": str(title).strip() if isinstance(title, str) and title.strip() else "Untitled",
        "description": str(description).strip() if isinstance(description, str) else "",
        "source": graph_source,
        "nodes": nodes,
        "edges": edges,
        "metadata": dict(metadata) if isinstance(metadata, dict) else {},
        "created_at": int(created_at) if isinstance(created_at, int) else now,
        "updated_at": int(updated_at) if isinstance(updated_at, int) else now,
    }
    return stamp_concat_video_nodes(
        drop_keyframe_to_keyframe_deps(ensure_bootstrap_pipeline(graph))
    )


def _append_unique_edge(
    edges: list[DesignerGraphEdge],
    *,
    edge_id: str,
    source: str,
    target: str,
    kind: str = EDGE_KIND_DATA,
    label: str | None = None,
) -> None:
    if any(edge.get("source") == source and edge.get("target") == target for edge in edges):
        return
    edge: DesignerGraphEdge = {"id": edge_id, "source": source, "target": target, "kind": kind}
    if label:
        edge["label"] = label
    edges.append(edge)


def _append_node_input(graph: DesignerExecutionGraph, node_id: str, input_id: str) -> None:
    for node in graph.get("nodes") or []:
        if node.get("id") != node_id:
            continue
        config = node.setdefault("config", {})
        if not isinstance(config, dict):
            return
        inputs = [str(item) for item in (config.get("inputs") or [])]
        if input_id not in inputs:
            inputs.append(input_id)
            config["inputs"] = inputs
        return


def ensure_bootstrap_pipeline(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Keep old bootstrap graphs on the current clip / scene / keyframe pipeline."""
    metadata = graph.get("metadata") or {}
    if metadata.get("bootstrap") != "designer.graph.bootstrap.v1":
        return graph
    node_ids = {node["id"] for node in graph.get("nodes") or []}
    edges = graph.setdefault("edges", [])
    has_frame = "n_frame" in node_ids or any(
        str(item).startswith("n_frame_") for item in node_ids
    )
    if "n_brief" in node_ids and has_frame and "n_scene" not in node_ids:
        graph.setdefault("nodes", []).append(
            {
                "id": "n_scene",
                "type": NODE_TYPE_IMAGE,
                "label": "Image",
                "config": {
                    "role": NODE_TYPE_IMAGE,
                    "pipeline": PIPELINE_SCENE,
                    "inputs": ["n_brief"],
                },
                "layout": {"x": 400, "y": 240, "width": 280, "height": 160},
            }
        )
        node_ids.add("n_scene")
    frame_ids = [
        str(node.get("id") or "")
        for node in graph.get("nodes") or []
        if node_pipeline(node) == PIPELINE_FRAME
        or str(node.get("id") or "") == "n_frame"
        or str(node.get("id") or "").startswith("n_frame_")
    ]
    frame_ids = [item for item in frame_ids if item]
    if "n_scene" in node_ids:
        _append_unique_edge(edges, edge_id="e_brief_scene", source="n_brief", target="n_scene")
        for frame_id in frame_ids:
            _append_unique_edge(
                edges,
                edge_id=f"e_scene_{frame_id}",
                source="n_scene",
                target=frame_id,
            )
            _append_node_input(graph, frame_id, "n_scene")
    if "n_scene" in node_ids and "n_storyboard" in node_ids:
        _append_unique_edge(
            edges,
            edge_id="e_scene_storyboard",
            source="n_scene",
            target="n_storyboard",
            kind=EDGE_KIND_SYNC,
            label="Align",
        )
    if frame_ids:
        clip_nodes = [
            node
            for node in graph.get("nodes") or []
            if node_pipeline(node) == PIPELINE_CLIP
            or str(node.get("id") or "") == "n_clip"
            or str(node.get("id") or "").startswith("n_clip_")
        ]
        if not clip_nodes and "n_clip" in node_ids:
            clip_nodes = [{"id": "n_clip"}]
        frame_by_shot = {node_shot_index(node): str(node.get("id") or "") for node in graph.get("nodes") or [] if str(node.get("id") or "") in frame_ids}
        for clip in clip_nodes:
            clip_id = str(clip.get("id") or "")
            if not clip_id:
                continue
            frame_id = frame_by_shot.get(node_shot_index(clip)) or frame_ids[0]
            _append_unique_edge(
                edges,
                edge_id=f"e_{frame_id}_{clip_id}",
                source=frame_id,
                target=clip_id,
            )
            _append_node_input(graph, clip_id, frame_id)
            if "n_scene" in node_ids:
                _append_unique_edge(
                    edges,
                    edge_id=f"e_scene_{clip_id}",
                    source="n_scene",
                    target=clip_id,
                )
                _append_node_input(graph, clip_id, "n_scene")
        clip_ids = [str(node.get("id") or "") for node in clip_nodes if node.get("id")]
        if clip_ids and "n_compose" not in node_ids:
            graph.setdefault("nodes", []).append(
                {
                    "id": "n_compose",
                    "type": NODE_TYPE_VIDEO,
                    "label": "Video",
                    "config": {
                        "role": NODE_TYPE_VIDEO,
                        "pipeline": PIPELINE_COMPOSE,
                        "inputs": list(clip_ids),
                    },
                    "layout": compose_layout_right_of_clips(
                        [node.get("layout") for node in clip_nodes]
                    ),
                }
            )
            node_ids.add("n_compose")
        if "n_compose" in {node["id"] for node in graph.get("nodes") or []}:
            for clip_id in clip_ids:
                _append_unique_edge(
                    edges,
                    edge_id=f"e_{clip_id}_compose",
                    source=clip_id,
                    target="n_compose",
                )
                _append_node_input(graph, "n_compose", clip_id)
    drop_keyframe_to_keyframe_deps(graph)
    return repair_overlapping_pipeline_layout(graph)


COMPOSE_NODE_ID = "n_compose"
DEFAULT_NODE_WIDTH = 280.0
DEFAULT_NODE_HEIGHT = 160.0
LAYOUT_COL_GAP = 80.0
LAYOUT_ROW_GAP = 40.0
LAYOUT_ROW_STEP = DEFAULT_NODE_HEIGHT + LAYOUT_ROW_GAP


def _layout_box(layout: dict[str, Any] | None) -> tuple[float, float, float, float]:
    raw = layout if isinstance(layout, dict) else {}
    x = float(raw.get("x") or 0)
    y = float(raw.get("y") or 0)
    width = float(raw.get("width") or DEFAULT_NODE_WIDTH)
    height = float(raw.get("height") or DEFAULT_NODE_HEIGHT)
    return x, y, width, height


def compose_layout_right_of_clips(clip_layouts: list[dict[str, Any] | None]) -> dict[str, float]:
    """Place 成片 to the right of the clip column, vertically centered on that stack."""
    boxes = [_layout_box(layout) for layout in clip_layouts if layout is not None]
    if not boxes:
        return {
            "x": 1480.0,
            "y": 240.0,
            "width": DEFAULT_NODE_WIDTH,
            "height": DEFAULT_NODE_HEIGHT,
        }
    clip_right = max(x + width for x, _y, width, _h in boxes)
    stack_top = min(y for _x, y, _w, _h in boxes)
    stack_bottom = max(y + height for _x, y, _w, height in boxes)
    y = stack_top + max(0.0, (stack_bottom - stack_top - DEFAULT_NODE_HEIGHT) / 2.0)
    return {
        "x": clip_right + LAYOUT_COL_GAP,
        "y": y,
        "width": DEFAULT_NODE_WIDTH,
        "height": DEFAULT_NODE_HEIGHT,
    }


def _stacked_node_layout(
    *,
    existing: dict[str, Any],
    base: dict[str, Any],
    index: int,
    default_x: float,
    default_y: float,
) -> dict[str, float]:
    origin_y = float(base.get("y", default_y))
    return {
        "x": float(existing.get("x", base.get("x", default_x))),
        "y": origin_y + (index - 1) * LAYOUT_ROW_STEP,
        "width": float(existing.get("width", base.get("width", DEFAULT_NODE_WIDTH))),
        "height": float(existing.get("height", base.get("height", DEFAULT_NODE_HEIGHT))),
    }


def _boxes_overlap(left: dict[str, Any], right: dict[str, Any]) -> bool:
    ax, ay, aw, ah = _layout_box(left.get("layout") if isinstance(left, dict) else None)
    bx, by, bw, bh = _layout_box(right.get("layout") if isinstance(right, dict) else None)
    return ax < bx + bw and bx < ax + aw and ay < by + bh and by < ay + ah


def _restack_column(nodes: list[dict[str, Any]]) -> None:
    visible = [node for node in nodes if isinstance(node, dict)]
    if len(visible) < 2:
        return
    if not any(
        _boxes_overlap(left, right)
        for index, left in enumerate(visible)
        for right in visible[index + 1 :]
    ):
        return
    ordered = sorted(visible, key=lambda node: _layout_box(node.get("layout"))[1])
    origin_x, origin_y, _width, _height = _layout_box(ordered[0].get("layout"))
    for index, node in enumerate(ordered):
        _x, _y, node_w, node_h = _layout_box(node.get("layout"))
        node["layout"] = {
            "x": origin_x,
            "y": origin_y + index * LAYOUT_ROW_STEP,
            "width": node_w,
            "height": node_h,
        }


def repair_overlapping_pipeline_layout(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Push 成片 to the right of clips and unstack overlapping bootstrap columns."""
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    _restack_column(
        [by_id[key] for key in ("n_character", "n_scene", "n_storyboard") if key in by_id]
    )
    frames = sorted(
        [
            node
            for node in nodes
            if node_pipeline(node) == PIPELINE_FRAME
            or str(node.get("id") or "").startswith("n_frame")
        ],
        key=node_shot_index,
    )
    clips = sorted(
        [
            node
            for node in nodes
            if node_pipeline(node) == PIPELINE_CLIP
            or str(node.get("id") or "").startswith("n_clip")
        ],
        key=node_shot_index,
    )
    _restack_column(frames)
    _restack_column(clips)
    compose = by_id.get(COMPOSE_NODE_ID)
    if compose and clips:
        clip_right = max(
            _layout_box(node.get("layout"))[0] + _layout_box(node.get("layout"))[2]
            for node in clips
        )
        compose_x = _layout_box(compose.get("layout"))[0]
        overlaps = any(_boxes_overlap(compose, clip) for clip in clips)
        if overlaps or compose_x < clip_right:
            compose["layout"] = compose_layout_right_of_clips(
                [node.get("layout") for node in clips]
            )
    return graph


def clip_node_id(shot_index: int) -> str:
    return f"n_clip_{int(shot_index)}"


def frame_node_id(shot_index: int) -> str:
    return f"n_frame_{int(shot_index)}"


def node_shot_index(node: DesignerGraphNode) -> int:
    raw = node_config(node).get("shot_index")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        if value > 0:
            return value
    node_id = str(node.get("id") or "")
    if node_id in {"n_clip", "n_frame"}:
        return 1
    for prefix in ("n_clip_", "n_frame_"):
        if node_id.startswith(prefix):
            suffix = node_id.rsplit("_", 1)[-1]
            if suffix.isdigit() and int(suffix) > 0:
                return int(suffix)
    return 1


def _copied_delegate(graph: DesignerExecutionGraph) -> str | None:
    for node in graph.get("nodes") or []:
        delegate = str(node_config(node).get("delegate") or "").strip()
        if delegate:
            return delegate
    return None


def _is_shot_pipeline_id(node_id: str) -> bool:
    return (
        node_id in {"n_frame", "n_clip", COMPOSE_NODE_ID}
        or node_id.startswith("n_frame_")
        or node_id.startswith("n_clip_")
    )


def _is_frame_pipeline_id(node_id: str) -> bool:
    return node_id == "n_frame" or node_id.startswith("n_frame_")


def _allowed_prior_frame_deps(node: DesignerGraphNode) -> set[str]:
    """Same-setting prompt-handoff wires that must survive normalize."""
    config = node.get("config")
    if not isinstance(config, dict):
        return set()
    irefs = config.get("identity_refs") if isinstance(config.get("identity_refs"), dict) else {}
    allowed: set[str] = set()
    for key in (
        "scene_prompt_handoff_from",
        "scene_master_frame_id",
        "prior_keyframe_node_id",
    ):
        for src in (config.get(key), irefs.get(key)):
            val = str(src or "").strip()
            if val and _is_frame_pipeline_id(val):
                allowed.add(val)
    # Only keep handoff deps when this node is a non-master keyframe.
    if bool(config.get("is_scene_master") or irefs.get("is_scene_master")):
        return set()
    return allowed


def drop_keyframe_to_keyframe_deps(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Strip accidental keyframe→keyframe edges; keep declared edit-prior / scene-master wires."""
    nodes = [n for n in (graph.get("nodes") or []) if isinstance(n, dict)]
    frame_ids = {
        str(node.get("id") or "")
        for node in nodes
        if (
            node_pipeline(node) == PIPELINE_FRAME
            or _is_frame_pipeline_id(str(node.get("id") or ""))
        )
        and str(node.get("id") or "")
    }
    if len(frame_ids) < 2:
        return graph
    keep_pairs: set[tuple[str, str]] = set()
    by_id = {str(n.get("id") or ""): n for n in nodes if n.get("id")}
    for nid, node in by_id.items():
        if nid not in frame_ids:
            continue
        for dep in _allowed_prior_frame_deps(node):
            if dep in frame_ids and dep != nid:
                keep_pairs.add((dep, nid))
    kept_edges = [
        edge
        for edge in (graph.get("edges") or [])
        if not (
            str(edge.get("source") or "") in frame_ids
            and str(edge.get("target") or "") in frame_ids
            and (str(edge.get("source") or ""), str(edge.get("target") or "")) not in keep_pairs
        )
    ]
    existing_pairs = {
        (str(e.get("source") or ""), str(e.get("target") or ""))
        for e in kept_edges
        if isinstance(e, dict)
    }
    for src, tgt in sorted(keep_pairs):
        if (src, tgt) not in existing_pairs:
            kept_edges.append(
                {
                    "id": f"e_{src}_{tgt}",
                    "source": src,
                    "target": tgt,
                    "kind": EDGE_KIND_DATA,
                }
            )
            existing_pairs.add((src, tgt))
    graph["edges"] = kept_edges
    for node in nodes:
        node_id = str(node.get("id") or "")
        if node_id not in frame_ids:
            continue
        config = node.get("config")
        if not isinstance(config, dict):
            continue
        allowed = {dep for dep in _allowed_prior_frame_deps(node) if dep != node_id}
        inputs = [str(item) for item in (config.get("inputs") or [])]
        next_inputs = [
            item for item in inputs if item not in frame_ids or item in allowed
        ]
        # Ensure declared prior/master stay wired even if a prior pass omitted them.
        for dep in allowed:
            if dep not in next_inputs:
                next_inputs.append(dep)
        if next_inputs != inputs:
            config["inputs"] = next_inputs
    return graph


_BOOTSTRAP_FRAME_IDS = frozenset({"n_frame", "n_frame_1"})
_BOOTSTRAP_CLIP_IDS = frozenset({"n_clip", "n_clip_1"})


def _shot_pipeline_ids(graph: DesignerExecutionGraph) -> tuple[set[str], set[str]]:
    frames: set[str] = set()
    clips: set[str] = set()
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        pipeline = node_pipeline(node)
        if pipeline == PIPELINE_FRAME or node_id == "n_frame" or node_id.startswith("n_frame_"):
            frames.add(node_id)
        elif pipeline == PIPELINE_CLIP or node_id == "n_clip" or node_id.startswith("n_clip_"):
            clips.add(node_id)
    return frames, clips


def shot_pipeline_count(graph: DesignerExecutionGraph) -> int:
    """How many per-shot frame/clip slots the graph currently has."""
    frames, clips = _shot_pipeline_ids(graph)
    return max(len(frames), len(clips), 0)


def is_one_shot_bootstrap_skeleton(graph: DesignerExecutionGraph) -> bool:
    """True when the graph still looks like the 1-shot bootstrap, not a user edit."""
    frames, clips = _shot_pipeline_ids(graph)
    if not frames and not clips:
        return False
    if any(node_id not in _BOOTSTRAP_FRAME_IDS for node_id in frames):
        return False
    if any(node_id not in _BOOTSTRAP_CLIP_IDS for node_id in clips):
        return False
    return len(frames) <= 1 and len(clips) <= 1


def _asset_ref_uri(ref: object) -> str:
    if not isinstance(ref, dict):
        return ""
    return str(ref.get("uri") or "").strip()


def preserve_node_output_refs(
    incoming: DesignerExecutionGraph,
    existing: DesignerExecutionGraph | None,
) -> DesignerExecutionGraph:
    """Keep completed artifact URIs when a layout save omits output_ref."""
    if existing is None:
        return incoming
    existing_by_id = {
        str(node.get("id") or ""): node
        for node in existing.get("nodes") or []
        if str(node.get("id") or "")
    }
    nodes: list[DesignerGraphNode] = []
    changed = False
    for node in incoming.get("nodes") or []:
        prev = existing_by_id.get(str(node.get("id") or "")) or {}
        prev_ref = prev.get("output_ref") if isinstance(prev, dict) else None
        incoming_uri = _asset_ref_uri(node.get("output_ref"))
        prev_uri = _asset_ref_uri(prev_ref)
        # A browser blob preview must not replace a file already stored for this node.
        if incoming_uri.startswith("blob:") and prev_uri.startswith("file:"):
            nodes.append({**node, "output_ref": dict(prev_ref)})
            changed = True
            continue
        if prev_uri and not incoming_uri:
            nodes.append({**node, "output_ref": dict(prev_ref)})
            changed = True
            continue
        nodes.append(node)
    if not changed:
        return incoming
    next_graph = dict(incoming)
    next_graph["nodes"] = nodes
    return next_graph


def preserve_expanded_shot_nodes(
    incoming: DesignerExecutionGraph,
    existing: DesignerExecutionGraph | None,
) -> DesignerExecutionGraph:
    """Keep already-expanded keyframe/clip nodes when a stale save sends the bootstrap shape.

    User edits that drop extra shots (Image 6 / Video 4) must persist. Only a
    1-shot bootstrap skeleton is grafted back onto an expanded graph.
    """
    if existing is None:
        return incoming
    incoming_meta = incoming.get("metadata") if isinstance(incoming.get("metadata"), dict) else {}
    if incoming_meta.get("user_topology_edit"):
        return incoming
    existing_count = shot_pipeline_count(existing)
    incoming_count = shot_pipeline_count(incoming)
    if (
        not is_one_shot_bootstrap_skeleton(incoming)
        or incoming_count > 1
        or existing_count <= incoming_count
        or existing_count <= 1
    ):
        return incoming
    incoming_ids = {str(node.get("id") or "") for node in incoming.get("nodes") or []}
    grafted = dict(incoming)
    extra_nodes = [
        dict(node)
        for node in existing.get("nodes") or []
        if str(node.get("id") or "") not in incoming_ids
        and (
            node_pipeline(node) in {PIPELINE_FRAME, PIPELINE_CLIP, PIPELINE_COMPOSE}
            or _is_shot_pipeline_id(str(node.get("id") or ""))
        )
    ]
    grafted["nodes"] = [dict(node) for node in incoming.get("nodes") or []] + extra_nodes
    return expand_shot_nodes(grafted, existing_count)


def preserve_nodes_added_since(
    incoming: DesignerExecutionGraph,
    existing: DesignerExecutionGraph | None,
) -> DesignerExecutionGraph:
    """Graft back nodes a concurrent writer added, when ``incoming`` is stale.

    Run orchestration reads a graph, awaits an agent, then saves its copy. While
    it waits, a node worker can rebuild the topology (expanded clips, the audio
    bed). Writing the older copy verbatim would delete those nodes, so any node
    the newer stored graph has is carried over along with its edges.
    """
    if existing is None:
        return incoming
    if int(incoming.get("updated_at") or 0) >= int(existing.get("updated_at") or 0):
        return incoming
    incoming_ids = {str(node.get("id") or "") for node in incoming.get("nodes") or []}
    extra = [
        dict(node)
        for node in existing.get("nodes") or []
        if str(node.get("id") or "") not in incoming_ids
    ]
    if not extra:
        return incoming
    live = incoming_ids | {str(node.get("id") or "") for node in extra}
    seen = {
        (str(edge.get("source") or ""), str(edge.get("target") or ""))
        for edge in incoming.get("edges") or []
    }
    extra_edges = []
    for edge in existing.get("edges") or []:
        pair = (str(edge.get("source") or ""), str(edge.get("target") or ""))
        if pair in seen or pair[0] not in live or pair[1] not in live:
            continue
        extra_edges.append(dict(edge))
        seen.add(pair)
    grafted = dict(incoming)
    grafted["nodes"] = [dict(node) for node in incoming.get("nodes") or []] + extra
    grafted["edges"] = [dict(edge) for edge in incoming.get("edges") or []] + extra_edges
    return grafted


def expand_shot_nodes(
    graph: DesignerExecutionGraph,
    shot_count: int,
) -> DesignerExecutionGraph:
    """One clip node per storyboard shot (scene-card R2V), plus compose."""
    count = max(1, int(shot_count or 1))
    raw = dict(graph)
    existing_by_id = {
        str(node.get("id") or ""): dict(node)
        for node in raw.get("nodes") or []
        if str(node.get("id") or "")
    }
    clip_template = existing_by_id.get("n_clip_1") or existing_by_id.get("n_clip") or {}
    clip_base = dict(clip_template.get("layout") or {})

    kept_nodes = [
        dict(node)
        for node in raw.get("nodes") or []
        if node_pipeline(node) not in {PIPELINE_FRAME, PIPELINE_CLIP, PIPELINE_COMPOSE}
        and not _is_shot_pipeline_id(str(node.get("id") or ""))
    ]
    kept_edges = [
        dict(edge)
        for edge in raw.get("edges") or []
        if not _is_shot_pipeline_id(str(edge.get("source") or ""))
        and not _is_shot_pipeline_id(str(edge.get("target") or ""))
    ]
    kept_ids = {str(node.get("id") or "") for node in kept_nodes}

    def _role_node_ids(role: str) -> list[str]:
        ids: list[str] = []
        for node in kept_nodes:
            if node_pipeline(node) == role and str(node.get("id") or ""):
                ids.append(str(node["id"]))
        return ids

    character_ids = _role_node_ids(NODE_ROLE_CHARACTER_DESIGN)
    scene_ids = _role_node_ids(NODE_ROLE_SCENE)
    storyboard_id = (_role_node_ids(NODE_ROLE_STORYBOARD) or [None])[0]
    brief_id = (_role_node_ids(NODE_ROLE_BRIEF) or [None])[0]
    delegate = _copied_delegate(graph)
    clip_nodes: list[DesignerGraphNode] = []
    for index in range(1, count + 1):
        clip_id = clip_node_id(index)
        clip_inputs: list[str] = []
        if brief_id:
            clip_inputs.append(brief_id)
        if storyboard_id:
            clip_inputs.append(storyboard_id)
        clip_inputs.extend(character_ids)
        clip_inputs.extend(scene_ids)
        if index > 1:
            clip_inputs.append(clip_node_id(index - 1))
        clip_inputs = list(dict.fromkeys([x for x in clip_inputs if x]))
        clip_config: dict[str, Any] = {
            "role": NODE_ROLE_CLIP,
            "pipeline": PIPELINE_CLIP,
            "shot_index": index,
            "keyframe_strategy": "clip_from_scene_and_solos",
            "inputs": clip_inputs,
        }
        prev_clip_generate = _generate_config(existing_by_id.get(clip_id) or {})
        if prev_clip_generate:
            clip_config[CONFIG_KEY_GENERATE] = prev_clip_generate
        if delegate:
            clip_config["delegate"] = delegate
        prev_clip_layout = dict((existing_by_id.get(clip_id) or {}).get("layout") or {})
        clip_layout = _stacked_node_layout(
            existing=prev_clip_layout,
            base=clip_base,
            index=index,
            default_x=1120.0,
            default_y=240.0,
        )
        from jiuwenswarm.server.runtime.designer.node_labels import (
            derive_shot_name,
            label_clip,
        )

        shot_name = derive_shot_name(
            {"title": f"Shot {index}", "action": ""},
            fallback_index=index,
        )
        clip_nodes.append(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": label_clip(
                    scene_number=1, clip_number=index, clip_name=shot_name
                ),
                "config": clip_config,
                "layout": clip_layout,
            }
        )
        for source_id in clip_inputs:
            if source_id not in kept_ids and not source_id.startswith("n_clip_"):
                continue
            kept_edges.append(
                {
                    "id": f"e_{source_id}_{clip_id}",
                    "source": source_id,
                    "target": clip_id,
                    "kind": EDGE_KIND_DATA,
                }
            )
        kept_edges.append(
            {
                "id": f"e_{clip_id}_compose",
                "source": clip_id,
                "target": COMPOSE_NODE_ID,
                "kind": EDGE_KIND_DATA,
            }
        )
    compose_inputs = [clip_node_id(index) for index in range(1, count + 1)]
    # Restore speech/music → compose so expand cannot orphan audio (or strip sound).
    for audio_id in ("n_speech", "n_music"):
        if audio_id in kept_ids:
            compose_inputs.append(audio_id)
            kept_edges.append(
                {
                    "id": f"e_{audio_id}_compose",
                    "source": audio_id,
                    "target": COMPOSE_NODE_ID,
                    "kind": EDGE_KIND_DATA,
                }
            )
    compose_config: dict[str, Any] = {
        "role": NODE_TYPE_VIDEO,
        "pipeline": PIPELINE_COMPOSE,
        "inputs": compose_inputs,
        "delegate": CONFIG_DELEGATE_HANDLER,
        "force_handler": True,
    }
    compose_node: DesignerGraphNode = {
        "id": COMPOSE_NODE_ID,
        "type": NODE_TYPE_VIDEO,
        "label": modality_node_label(NODE_TYPE_VIDEO, count + 1),
        "config": compose_config,
        "layout": compose_layout_right_of_clips(
            [node.get("layout") for node in clip_nodes]
        ),
    }
    raw["nodes"] = kept_nodes + clip_nodes + [compose_node]
    raw["edges"] = kept_edges
    return normalize_execution_graph(raw)


def _generate_config(node: DesignerGraphNode) -> dict[str, Any]:
    raw = node_config(node).get(CONFIG_KEY_GENERATE)
    return dict(raw) if isinstance(raw, dict) else {}


def apply_shot_generate_prompts(
    graph: DesignerExecutionGraph,
    prompts: list[str],
) -> DesignerExecutionGraph:
    """Fill frame/clip generate.prompt from storyboard rows unless the user edited them."""
    changed = False
    nodes: list[DesignerGraphNode] = []
    for node in graph.get("nodes") or []:
        pipeline = node_pipeline(node)
        if pipeline not in {PIPELINE_FRAME, PIPELINE_CLIP}:
            nodes.append(node)
            continue
        index = node_shot_index(node)
        if index < 1 or index > len(prompts):
            nodes.append(node)
            continue
        prompt_text = str(prompts[index - 1] or "").strip()
        if not prompt_text:
            nodes.append(node)
            continue
        generate = _generate_config(node)
        existing = str(generate.get("prompt") or "").strip()
        origin = str(generate.get("prompt_origin") or "").strip()
        if origin == GENERATE_PROMPT_ORIGIN_USER and existing:
            nodes.append(node)
            continue
        if existing == prompt_text and origin == GENERATE_PROMPT_ORIGIN_STORYBOARD:
            nodes.append(node)
            continue
        next_config = dict(node_config(node))
        next_generate = dict(generate)
        next_generate["prompt"] = prompt_text
        next_generate["prompt_origin"] = GENERATE_PROMPT_ORIGIN_STORYBOARD
        next_config[CONFIG_KEY_GENERATE] = next_generate
        nodes.append({**node, "config": next_config})
        changed = True
    if not changed:
        return graph
    raw = dict(graph)
    raw["nodes"] = nodes
    return normalize_execution_graph(raw)


def expand_clip_nodes_for_shots(
    graph: DesignerExecutionGraph,
    shot_count: int,
) -> DesignerExecutionGraph:
    """Backward-compatible alias: expand keyframes and clips together."""
    return expand_shot_nodes(graph, shot_count)


def ensure_bootstrap_clip_waits_for_frame(
    graph: DesignerExecutionGraph,
) -> DesignerExecutionGraph:
    """Backward-compatible alias used by older tests."""
    return ensure_bootstrap_pipeline(graph)


def is_leader_node_id(node_id: Any) -> bool:
    return str(node_id or "").strip() == LEADER_NODE_ID


def clip_activity_text(value: Any, *, max_len: int = ACTIVITY_TEXT_MAX) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)] + "…"


def format_activity_line(kind: str, text: str, tool: str = "") -> str:
    label = str(tool or "").strip() or str(kind or "").strip() or "activity"
    body = clip_activity_text(text) or label
    if kind == ACTIVITY_KIND_TOOL_CALL and str(tool or "").strip():
        return f"{tool} · {body}" if body != tool else tool
    return body


def normalize_node_activity(raw: Any) -> DesignerNodeActivity | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node_state.activity must be an object")
    kind = str(raw.get("kind") or ACTIVITY_KIND_STAGE).strip() or ACTIVITY_KIND_STAGE
    if kind not in ACTIVITY_KINDS:
        kind = ACTIVITY_KIND_STAGE
    text = clip_activity_text(raw.get("text"))
    tool = str(raw.get("tool") or "").strip()
    at = raw.get("at")
    activity: DesignerNodeActivity = {"kind": kind, "text": text}
    if tool:
        activity["tool"] = tool
    if isinstance(at, int) and not isinstance(at, bool):
        activity["at"] = at
    else:
        activity["at"] = utc_now_ms()
    return activity


def apply_node_activity(
    state: DesignerNodeState | dict[str, Any] | None,
    *,
    kind: str,
    text: str,
    tool: str = "",
    at: int | None = None,
) -> DesignerNodeState:
    current = dict(state or {})
    activity = normalize_node_activity(
        {
            "kind": kind,
            "text": text,
            "tool": tool,
            "at": at if isinstance(at, int) else utc_now_ms(),
        }
    )
    assert activity is not None
    line = format_activity_line(
        str(activity.get("kind") or ""),
        str(activity.get("text") or ""),
        str(activity.get("tool") or ""),
    )
    tail = [item for item in (current.get("activity_tail") or []) if isinstance(item, str)]
    if line and (not tail or tail[-1] != line):
        tail.append(line)
    log = [item for item in (current.get("activity_log") or []) if isinstance(item, dict)]
    signature = (
        str(activity.get("kind") or ""),
        str(activity.get("text") or ""),
        str(activity.get("tool") or ""),
    )
    previous_signature = (
        str(log[-1].get("kind") or ""),
        str(log[-1].get("text") or ""),
        str(log[-1].get("tool") or ""),
    ) if log else None
    if signature != previous_signature:
        log.append(activity)
    current["activity"] = activity
    current["activity_tail"] = tail[-ACTIVITY_TAIL_LIMIT:]
    current["activity_log"] = log[-ACTIVITY_LOG_LIMIT:]
    if "status" not in current:
        current["status"] = NODE_STATUS_RUNNING
    return current  # type: ignore[return-value]


def normalize_node_state(raw: Any) -> DesignerNodeState:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node_state must be an object")
    status = _require_str(raw.get("status"), "node_state.status")
    if status not in NODE_STATUSES:
        raise DesignerGraphValidationError(f"unsupported node status: {status!r}")
    state: DesignerNodeState = {"status": status}
    for key in ("started_at", "completed_at"):
        val = raw.get(key)
        if val is None:
            state[key] = None  # type: ignore[literal-required]
        elif isinstance(val, int) and not isinstance(val, bool):
            state[key] = val  # type: ignore[literal-required]
        else:
            raise DesignerGraphValidationError(f"node_state.{key} must be an integer or null")
    if "output_ref" in raw:
        state["output_ref"] = normalize_asset_ref(raw.get("output_ref"))
    if "output_refs" in raw:
        raw_refs = raw.get("output_refs")
        if not isinstance(raw_refs, list):
            raise DesignerGraphValidationError("node_state.output_refs must be an array")
        state["output_refs"] = [
            ref
            for ref in (normalize_asset_ref(item) for item in raw_refs)
            if ref is not None
        ]
    if "candidate_output_ref" in raw:
        state["candidate_output_ref"] = normalize_asset_ref(raw.get("candidate_output_ref"))
    if "candidate_output_refs" in raw:
        raw_candidates = raw.get("candidate_output_refs")
        if not isinstance(raw_candidates, list):
            raise DesignerGraphValidationError("node_state.candidate_output_refs must be an array")
        state["candidate_output_refs"] = [
            ref
            for ref in (normalize_asset_ref(item) for item in raw_candidates)
            if ref is not None
        ]
    error = raw.get("error")
    if isinstance(error, str):
        state["error"] = error
    blocked_by = raw.get("blocked_by")
    if blocked_by is not None:
        if not isinstance(blocked_by, list) or not all(
            isinstance(item, str) for item in blocked_by
        ):
            raise DesignerGraphValidationError("node_state.blocked_by must be a string array")
        state["blocked_by"] = list(blocked_by)
    activity = normalize_node_activity(raw.get("activity"))
    if activity is not None:
        state["activity"] = activity
    tail = raw.get("activity_tail")
    if tail is not None:
        if not isinstance(tail, list) or not all(isinstance(item, str) for item in tail):
            raise DesignerGraphValidationError("node_state.activity_tail must be a string array")
        state["activity_tail"] = [item for item in tail if item.strip()][:ACTIVITY_TAIL_LIMIT]
    log = raw.get("activity_log")
    if log is not None:
        if not isinstance(log, list):
            raise DesignerGraphValidationError("node_state.activity_log must be an array")
        state["activity_log"] = [
            item
            for item in (normalize_node_activity(entry) for entry in log)
            if item is not None
        ][-ACTIVITY_LOG_LIMIT:]
    return state


def is_leader_node_id(node_id: Any) -> bool:
    return str(node_id or "").strip() == LEADER_NODE_ID


def clip_activity_text(value: Any, *, max_len: int = ACTIVITY_TEXT_MAX) -> str:
    text = re.sub(r"\s+", " ", str(value or "")).strip()
    if len(text) <= max_len:
        return text
    return text[: max(1, max_len - 1)] + "…"


def format_activity_line(kind: str, text: str, tool: str = "") -> str:
    label = str(tool or "").strip() or str(kind or "").strip() or "activity"
    body = clip_activity_text(text) or label
    if kind == ACTIVITY_KIND_TOOL_CALL and str(tool or "").strip():
        return f"{tool} · {body}" if body != tool else tool
    return body


def normalize_node_activity(raw: Any) -> DesignerNodeActivity | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("node_state.activity must be an object")
    kind = str(raw.get("kind") or ACTIVITY_KIND_STAGE).strip() or ACTIVITY_KIND_STAGE
    if kind not in ACTIVITY_KINDS:
        kind = ACTIVITY_KIND_STAGE
    text = clip_activity_text(raw.get("text"))
    tool = str(raw.get("tool") or "").strip()
    at = raw.get("at")
    activity: DesignerNodeActivity = {"kind": kind, "text": text}
    if tool:
        activity["tool"] = tool
    if isinstance(at, int) and not isinstance(at, bool):
        activity["at"] = at
    else:
        activity["at"] = utc_now_ms()
    return activity


def apply_node_activity(
    state: DesignerNodeState | dict[str, Any] | None,
    *,
    kind: str,
    text: str,
    tool: str = "",
    at: int | None = None,
) -> DesignerNodeState:
    current = dict(state or {})
    activity = normalize_node_activity(
        {
            "kind": kind,
            "text": text,
            "tool": tool,
            "at": at if isinstance(at, int) else utc_now_ms(),
        }
    )
    assert activity is not None
    line = format_activity_line(
        str(activity.get("kind") or ""),
        str(activity.get("text") or ""),
        str(activity.get("tool") or ""),
    )
    tail = [item for item in (current.get("activity_tail") or []) if isinstance(item, str)]
    if line and (not tail or tail[-1] != line):
        tail.append(line)
    log = [item for item in (current.get("activity_log") or []) if isinstance(item, dict)]
    signature = (
        str(activity.get("kind") or ""),
        str(activity.get("text") or ""),
        str(activity.get("tool") or ""),
    )
    previous_signature = (
        str(log[-1].get("kind") or ""),
        str(log[-1].get("text") or ""),
        str(log[-1].get("tool") or ""),
    ) if log else None
    if signature != previous_signature:
        log.append(activity)
    current["activity"] = activity
    current["activity_tail"] = tail[-ACTIVITY_TAIL_LIMIT:]
    current["activity_log"] = log[-ACTIVITY_LOG_LIMIT:]
    if "status" not in current:
        current["status"] = NODE_STATUS_RUNNING
    return current  # type: ignore[return-value]


def normalize_execution_run(raw: Any) -> DesignerExecutionRun:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("run must be an object")
    schema_version = _require_str(raw.get("schema_version"), "schema_version")
    if schema_version != RUN_SCHEMA_VERSION:
        raise DesignerGraphValidationError(
            f"unsupported run schema_version: {schema_version!r} "
            f"(expected {RUN_SCHEMA_VERSION!r})"
        )
    run_id = _require_str(raw.get("run_id"), "run_id")
    graph_id = _require_str(raw.get("graph_id"), "graph_id")
    project_id = _require_str(raw.get("project_id"), "project_id")
    status = _require_str(raw.get("status"), "status")
    if status not in RUN_STATUSES:
        raise DesignerGraphValidationError(f"unsupported run status: {status!r}")
    raw_states = raw.get("node_states")
    if not isinstance(raw_states, dict):
        raise DesignerGraphValidationError("node_states must be an object")
    node_states = {
        str(node_id): normalize_node_state(state)
        for node_id, state in raw_states.items()
    }
    current_node_ids = raw.get("current_node_ids")
    if current_node_ids is None:
        current_ids: list[str] = []
    elif not isinstance(current_node_ids, list) or not all(
        isinstance(item, str) for item in current_node_ids
    ):
        raise DesignerGraphValidationError("current_node_ids must be a string array")
    else:
        current_ids = list(current_node_ids)
    now = utc_now_ms()
    created_at = raw.get("created_at")
    updated_at = raw.get("updated_at")
    out: DesignerExecutionRun = {
        "schema_version": RUN_SCHEMA_VERSION,
        "run_id": run_id,
        "graph_id": graph_id,
        "project_id": project_id,
        "status": status,
        "node_states": node_states,
        "current_node_ids": current_ids,
        "created_at": int(created_at) if isinstance(created_at, int) else now,
        "updated_at": int(updated_at) if isinstance(updated_at, int) else now,
    }
    error = raw.get("error")
    if isinstance(error, str) and error.strip():
        out["error"] = error.strip()[:500]
    warning = raw.get("warning")
    if isinstance(warning, str) and warning.strip():
        out["warning"] = warning.strip()[:500]
    raw_warnings = raw.get("warnings")
    if isinstance(raw_warnings, list):
        warnings = [
            str(item).strip()
            for item in raw_warnings
            if isinstance(item, str) and str(item).strip()
        ]
        if warnings:
            out["warnings"] = warnings[:8]
    metadata = raw.get("metadata")
    if isinstance(metadata, dict):
        out["metadata"] = dict(metadata)
    return out


def initial_node_states(graph: DesignerExecutionGraph) -> dict[str, DesignerNodeState]:
    return {node["id"]: {"status": NODE_STATUS_PENDING} for node in graph["nodes"]}


def node_config(node: DesignerGraphNode) -> DesignerNodeConfig:
    config = node.get("config")
    return config if isinstance(config, dict) else {}


def node_role(node: DesignerGraphNode) -> str:
    """Canvas modality (image / video / audio / text / table)."""
    role = str(node_config(node).get(CONFIG_KEY_ROLE) or "").strip()
    if role in NODE_TYPES:
        return role
    if role in PIPELINE_TO_TYPE:
        return PIPELINE_TO_TYPE[role]
    node_type = str(node.get("type") or "").strip()
    if node_type in NODE_TYPES:
        return node_type
    return role


def node_pipeline(node: DesignerGraphNode) -> str:
    """Internal generation recipe. Empty for generic user-added media nodes."""
    config = node_config(node)
    pipeline = str(config.get(CONFIG_KEY_PIPELINE) or "").strip()
    if pipeline in PIPELINES:
        return pipeline
    role = str(config.get(CONFIG_KEY_ROLE) or "").strip()
    if role in PIPELINES:
        return role
    return infer_pipeline_from_id(str(node.get("id") or ""))


def node_delegate(node: DesignerGraphNode) -> str:
    delegate = str(node_config(node).get("delegate") or CONFIG_DELEGATE_AGENT).strip()
    if delegate == CONFIG_DELEGATE_SUBAGENT:
        return CONFIG_DELEGATE_AGENT
    return delegate if delegate in CONFIG_DELEGATES else CONFIG_DELEGATE_AGENT


def node_agent_template(node: DesignerGraphNode) -> str:
    """Resolve the AgentTemplate ref for a node (explicit, else pipeline / type default)."""
    explicit = str(node_config(node).get(CONFIG_KEY_AGENT_TEMPLATE) or "").strip()
    if explicit:
        return explicit
    pipeline = node_pipeline(node)
    if pipeline in ROLE_DEFAULT_TEMPLATES:
        return ROLE_DEFAULT_TEMPLATES[pipeline]
    return ROLE_DEFAULT_TEMPLATES.get(node_role(node), "")


def node_uses_agent_runtime(node: DesignerGraphNode) -> bool:
    return node_delegate(node) != CONFIG_DELEGATE_HANDLER


CONFIG_KEY_IS_COMFYUI = "is_comfyui"
CONFIG_KEY_COMFYUI = "comfyui"


def is_comfyui_node(node: Any) -> bool:
    """Imported from a ComfyUI workflow: always a vLLM-Omni handler, never an agent."""
    return isinstance(node, dict) and bool(node_config(node).get(CONFIG_KEY_IS_COMFYUI))


def graph_uses_agent_scheduler(graph: DesignerExecutionGraph) -> bool:
    nodes = graph.get("nodes") or []
    if not nodes:
        return False
    return any(node_uses_agent_runtime(node) for node in nodes)


def apply_graph_patch(graph: DesignerExecutionGraph, patch: Any) -> DesignerExecutionGraph:
    """Merge upserts/removals into a domain graph, then re-normalize."""
    if patch is None:
        patch = {}
    if not isinstance(patch, dict):
        raise DesignerGraphValidationError("patch must be an object")
    raw = dict(graph)
    title = patch.get("title")
    if isinstance(title, str) and title.strip():
        raw["title"] = title.strip()
    if "description" in patch:
        description = patch.get("description")
        if description is not None and not isinstance(description, str):
            raise DesignerGraphValidationError("patch.description must be a string")
        raw["description"] = str(description or "")
    nodes_by_id = {node["id"]: dict(node) for node in raw.get("nodes") or []}
    upsert_nodes = patch.get("upsert_nodes") or []
    if not isinstance(upsert_nodes, list):
        raise DesignerGraphValidationError("patch.upsert_nodes must be an array")
    for item in upsert_nodes:
        node = normalize_node(item)
        nodes_by_id[node["id"]] = node
    remove_node_ids = patch.get("remove_node_ids") or []
    if not isinstance(remove_node_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in remove_node_ids
    ):
        raise DesignerGraphValidationError("patch.remove_node_ids must be a string array")
    for node_id in remove_node_ids:
        nodes_by_id.pop(str(node_id).strip(), None)
    remaining_node_ids = set(nodes_by_id)
    edges_by_id = {edge["id"]: dict(edge) for edge in raw.get("edges") or []}
    upsert_edges = patch.get("upsert_edges") or []
    if not isinstance(upsert_edges, list):
        raise DesignerGraphValidationError("patch.upsert_edges must be an array")
    for item in upsert_edges:
        edge = normalize_edge(item)
        edges_by_id[edge["id"]] = edge
    remove_edge_ids = patch.get("remove_edge_ids") or []
    if not isinstance(remove_edge_ids, list) or not all(
        isinstance(item, str) and item.strip() for item in remove_edge_ids
    ):
        raise DesignerGraphValidationError("patch.remove_edge_ids must be a string array")
    for edge_id in remove_edge_ids:
        edges_by_id.pop(str(edge_id).strip(), None)
    raw["nodes"] = list(nodes_by_id.values())
    raw["edges"] = [
        edge
        for edge in edges_by_id.values()
        if edge.get("source") in remaining_node_ids and edge.get("target") in remaining_node_ids
    ]
    raw["updated_at"] = utc_now_ms()
    return normalize_execution_graph(raw)


def edge_kind(edge: DesignerGraphEdge) -> str:
    kind = str(edge.get("kind") or EDGE_KIND_DATA).strip()
    return kind if kind in EDGE_KINDS else EDGE_KIND_DATA


def is_concat_video_source(node: DesignerGraphNode | dict[str, Any] | None) -> bool:
    """True when this node can feed ffmpeg concat (clip / generic video / film)."""
    if not isinstance(node, dict):
        return False
    nid = str(node.get("id") or "").strip()
    if not nid:
        return False
    pipeline = node_pipeline(node)
    ntype = str(node.get("type") or "").strip().lower()
    if pipeline == PIPELINE_CLIP or nid.startswith("n_clip"):
        return True
    if pipeline == PIPELINE_COMPOSE or nid in {"n_compose", "n_final"}:
        return True
    return ntype == NODE_TYPE_VIDEO


def video_concat_source_ids(graph: DesignerExecutionGraph, target_id: str) -> list[str]:
    """Direct video predecessors of ``target_id`` in input / edge order."""
    target = str(target_id or "").strip()
    if not target:
        return []
    by_id = {
        str(node.get("id") or ""): node
        for node in (graph.get("nodes") or [])
        if isinstance(node, dict) and node.get("id")
    }
    target_node = by_id.get(target)
    declared: list[str] = []
    if isinstance(target_node, dict):
        cfg = target_node.get("config") if isinstance(target_node.get("config"), dict) else {}
        declared = [str(item).strip() for item in (cfg.get("inputs") or []) if str(item).strip()]
    edged: list[str] = []
    for edge in graph.get("edges") or []:
        if not isinstance(edge, dict) or edge_kind(edge) != EDGE_KIND_DATA:
            continue
        if str(edge.get("target") or "") != target:
            continue
        source = str(edge.get("source") or "").strip()
        if source:
            edged.append(source)
    ordered: list[str] = []
    seen: set[str] = set()
    for source in [*declared, *edged]:
        if not source or source == target or source in seen:
            continue
        seen.add(source)
        ordered.append(source)
    return [
        source
        for source in ordered
        if is_concat_video_source(by_id.get(source))
    ]


def stamp_concat_video_nodes(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """User-added video nodes that already have video inputs assemble via compose."""
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or "").strip()
        if not nid or str(node.get("type") or "") != NODE_TYPE_VIDEO:
            continue
        pipeline = node_pipeline(node)
        if pipeline == PIPELINE_CLIP or nid.startswith("n_clip"):
            continue
        if pipeline == PIPELINE_COMPOSE:
            continue
        sources = video_concat_source_ids(graph, nid)
        if not sources:
            continue
        cfg = dict(node.get("config") or {}) if isinstance(node.get("config"), dict) else {}
        cfg["pipeline"] = PIPELINE_COMPOSE
        cfg.setdefault("role", NODE_TYPE_VIDEO)
        tools = [str(item) for item in (cfg.get("tools") or []) if str(item).strip()]
        if "ffmpeg_compose" not in tools:
            cfg["tools"] = [
                "call_model",
                "read_upstream",
                "ffmpeg_compose",
                "mix_audio",
            ]
        node["config"] = cfg
    return graph


def data_predecessors(graph: DesignerExecutionGraph) -> dict[str, list[str]]:
    incoming: dict[str, list[str]] = {node["id"]: [] for node in graph.get("nodes", [])}
    for edge in graph.get("edges", []):
        if edge_kind(edge) != EDGE_KIND_DATA:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and target in incoming:
            incoming[target].append(source)
    return incoming


def _node_schedule_priority(node: DesignerGraphNode | dict[str, Any]) -> tuple[int, int, str]:
    """Lower tuple = should finish earlier when breaking dependency cycles."""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    role = str(cfg.get("role") or cfg.get("pipeline") or node.get("type") or "").strip().lower()
    role_rank = {
        "text": 10,
        "brief": 10,
        "table": 20,
        "storyboard": 20,
        "character": 30,
        "character_design": 30,
        "scene": 40,
        "image": 45,
        "frame": 50,
        "keyframe": 50,
        "speech": 55,
        "tts": 55,
        "audio": 55,
        # The BGM bed is mixed after concat, so it must not take a slot from the
        # clips that the film actually waits on.
        "music": 65,
        "audio_bed": 65,
        "clip": 60,
        "video": 60,
        "compose": 70,
        "film": 70,
    }.get(role, 50)
    try:
        shot = int(cfg.get("shot_index") or 0)
    except (TypeError, ValueError):
        shot = 0
    return (role_rank, max(0, shot), str(node.get("id") or ""))


def _config_soft_predecessors(node: DesignerGraphNode | dict[str, Any]) -> list[str]:
    """Declared deps on the node config that may not yet be mirrored as edges."""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    ids: list[str] = []
    for key in (
        "inputs",
        "character_node_ids",
    ):
        raw = cfg.get(key)
        if isinstance(raw, list):
            ids.extend(str(x) for x in raw if str(x).strip())
    for key in (
        "continuity_clip_node_id",
        "previous_clip_node_id",
        "scene_prompt_handoff_from",
        "prior_keyframe_node_id",
        "scene_master_frame_id",
        "continuity_frame_node_id",
        "master_scene_node_id",
    ):
        val = str(cfg.get(key) or "").strip()
        if val:
            ids.append(val)
    identity = cfg.get("identity_refs") if isinstance(cfg.get("identity_refs"), dict) else {}
    for key in (
        "scene_prompt_handoff_from",
        "prior_keyframe_node_id",
        "scene_master_frame_id",
        "scene_node_id",
    ):
        val = str(identity.get(key) or "").strip()
        if val:
            ids.append(val)
    for cid in identity.get("character_node_ids") or []:
        if str(cid).strip():
            ids.append(str(cid))
    self_id = str(node.get("id") or "")
    out: list[str] = []
    seen: set[str] = set()
    for item in ids:
        if not item or item == self_id or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return out


def raw_execution_predecessors(graph: DesignerExecutionGraph) -> dict[str, list[str]]:
    """Union of data-edge preds and config-declared soft deps (may contain cycles)."""
    incoming = data_predecessors(graph)
    known = set(incoming)
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or "")
        if not nid or nid not in known:
            continue
        for pred in _config_soft_predecessors(node):
            if pred in known and pred not in incoming[nid]:
                incoming[nid].append(pred)
    return incoming


def _strongly_connected_components(preds: dict[str, list[str]]) -> list[list[str]]:
    """Tarjan SCC over nodes with directed edges pred → node."""
    nodes = list(preds.keys())
    succ: dict[str, list[str]] = {n: [] for n in nodes}
    for nid, parents in preds.items():
        for p in parents:
            if p in succ:
                succ[p].append(nid)

    index = 0
    stack: list[str] = []
    on_stack: set[str] = set()
    indices: dict[str, int] = {}
    lowlink: dict[str, int] = {}
    components: list[list[str]] = []

    def strongconnect(v: str) -> None:
        nonlocal index
        indices[v] = index
        lowlink[v] = index
        index += 1
        stack.append(v)
        on_stack.add(v)
        for w in succ.get(v, []):
            if w not in indices:
                strongconnect(w)
                lowlink[v] = min(lowlink[v], lowlink[w])
            elif w in on_stack:
                lowlink[v] = min(lowlink[v], indices[w])
        if lowlink[v] == indices[v]:
            comp: list[str] = []
            while True:
                w = stack.pop()
                on_stack.discard(w)
                comp.append(w)
                if w == v:
                    break
            components.append(comp)

    for v in nodes:
        if v not in indices:
            strongconnect(v)
    return components


def break_cycles_for_schedule(
    preds: dict[str, list[str]],
    graph: DesignerExecutionGraph,
) -> dict[str, list[str]]:
    """Drop back-edges inside SCCs so dependents never schedule before dependencies.

    Within a cycle, keep edges from earlier schedule-priority → later priority and
    drop the reverse, so shot/role order decides who may run first.
    """
    by_id = {
        str(n.get("id") or ""): n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }
    priority = {
        nid: _node_schedule_priority(by_id[nid]) if nid in by_id else (50, 0, nid)
        for nid in preds
    }
    scc_id: dict[str, int] = {}
    for i, comp in enumerate(_strongly_connected_components(preds)):
        for nid in comp:
            scc_id[nid] = i

    dag: dict[str, list[str]] = {nid: [] for nid in preds}
    for nid, parents in preds.items():
        kept: list[str] = []
        for pred in parents:
            if pred not in preds:
                continue
            if scc_id.get(pred) != scc_id.get(nid):
                kept.append(pred)
                continue
            # Same SCC (cycle): only keep forward priority edges.
            if priority[pred] < priority[nid]:
                kept.append(pred)
        # Stable unique
        seen: set[str] = set()
        for pred in kept:
            if pred not in seen:
                seen.add(pred)
                dag[nid].append(pred)
    return dag


def execution_predecessors(graph: DesignerExecutionGraph) -> dict[str, list[str]]:
    """Schedule predecessors: edges + config deps, cycles broken safely."""
    return break_cycles_for_schedule(raw_execution_predecessors(graph), graph)


def _pipeline_role(node: DesignerGraphNode | dict[str, Any] | None) -> str:
    if not isinstance(node, dict):
        return ""
    cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    return str(
        cfg.get("role") or cfg.get("pipeline") or node.get("type") or ""
    ).strip().lower()


def is_compose_sink_node(node: DesignerGraphNode | dict[str, Any] | None) -> bool:
    """True for the final ffmpeg/film assemble node."""
    if not isinstance(node, dict):
        return False
    nid = str(node.get("id") or "").strip().lower()
    if nid in {"n_compose", "n_final"} or nid.startswith("n_compose"):
        return True
    role = _pipeline_role(node)
    return role in {"compose", "film"}


def compose_required_predecessor_ids(graph: DesignerExecutionGraph) -> list[str]:
    """Every clip + separate speech/music node that must finish before ffmpeg compose.

    Edges/inputs alone can drift; this always collects live media nodes so compose
    cannot start after only the last clip or a soft prompt handoff.
    """
    compose_ids = {
        str(node.get("id") or "")
        for node in graph.get("nodes") or []
        if is_compose_sink_node(node) and str(node.get("id") or "")
    }
    connected_to_compose: set[str] = set()
    for node in graph.get("nodes") or []:
        if str(node.get("id") or "") not in compose_ids:
            continue
        cfg = node.get("config") if isinstance(node.get("config"), dict) else {}
        connected_to_compose.update(
            str(item).strip() for item in (cfg.get("inputs") or []) if str(item).strip()
        )
    for edge in graph.get("edges") or []:
        if str(edge.get("target") or "") in compose_ids:
            source = str(edge.get("source") or "").strip()
            if source:
                connected_to_compose.add(source)

    out: list[str] = []
    seen: set[str] = set()
    for node in graph.get("nodes") or []:
        if not isinstance(node, dict):
            continue
        nid = str(node.get("id") or "").strip()
        if not nid or nid in seen:
            continue
        role = _pipeline_role(node)
        pipeline = ""
        try:
            pipeline = str(node_pipeline(node) or "").strip().lower()  # type: ignore[misc]
        except Exception:  # noqa: BLE001
            pipeline = role
        is_clip = (
            role in {"clip"}
            or pipeline == "clip"
            or (nid.startswith("n_clip") and role not in {"compose", "film"})
        )
        is_audio = role in {"speech", "tts", "music", "audio", "audio_bed"} or pipeline in {
            "speech",
            "music",
        }
        if is_clip or (is_audio and nid in connected_to_compose):
            seen.add(nid)
            out.append(nid)
    # Stable: clips by shot, then audio ids.
    def _sort_key(nid: str) -> tuple[int, int, str]:
        by_id = {
            str(n.get("id") or ""): n
            for n in (graph.get("nodes") or [])
            if isinstance(n, dict)
        }
        n = by_id.get(nid) or {}
        role = _pipeline_role(n)
        cfg = n.get("config") if isinstance(n.get("config"), dict) else {}
        try:
            shot = int(cfg.get("shot_index") or 0)
        except (TypeError, ValueError):
            shot = 0
        kind = 0 if role in {"clip"} or nid.startswith("n_clip") else 1
        return (kind, shot, nid)

    return sorted(out, key=_sort_key)


def is_soft_artifact_dependency(
    graph: DesignerExecutionGraph,
    pred_id: str,
    node_id: str,
) -> bool:
    """True when the dependent only needs an early artifact (e.g. Wan prompt), not full completion.

    Clip→shot consistency and same-setting scene-prompt handoffs are soft: once the
    upstream node has published its prompt, the downstream node may start even while
    upstream media generation is still running.

    Compose/ffmpeg is NEVER soft — it must wait for all clips (and separate audio).
    """
    by_id = {
        str(n.get("id") or ""): n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }
    pred = by_id.get(str(pred_id or ""))
    node = by_id.get(str(node_id or ""))
    if pred is None or node is None:
        return False
    # Final film assemble always hard-waits for media completion.
    if is_compose_sink_node(node):
        return False
    prole = _pipeline_role(pred)
    nrole = _pipeline_role(node)
    ncfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    pcfg = pred.get("config") if isinstance(pred.get("config"), dict) else {}

    # Only real clip leaves (role=clip), never type=video compose sinks.
    if nrole == "clip" and prole == "clip":
        return True

    frame_roles = {"frame", "keyframe"}

    # Same-setting KF handoff: later compose KFs need master scene prompt text.
    if nrole in frame_roles and prole in frame_roles:
        handoff = str(
            ncfg.get("scene_prompt_handoff_from")
            or ncfg.get("scene_master_frame_id")
            or ""
        ).strip()
        if handoff == str(pred_id):
            return True
        if str(ncfg.get("continuity_frame_node_id") or "").strip() == str(pred_id):
            strategy = str(
                ncfg.get("keyframe_strategy")
                or ((ncfg.get("identity_refs") or {}) if isinstance(ncfg.get("identity_refs"), dict) else {}).get(
                    "keyframe_strategy"
                )
                or ""
            )
            if "compose" in strategy or strategy == "compose_from_solo_refs":
                return True
        return False

    # Clip depending on another shot's frame only for scene/prompt text (not own KF image).
    if nrole == "clip" and prole in frame_roles:
        try:
            own_shot = int(ncfg.get("shot_index") or 0)
        except (TypeError, ValueError):
            own_shot = 0
        try:
            pred_shot = int(pcfg.get("shot_index") or 0)
        except (TypeError, ValueError):
            pred_shot = 0
        if own_shot and (pred_shot == own_shot or str(pred_id) == f"n_frame_{own_shot}"):
            return False  # own keyframe image is a hard media dep
        soft_ids = {
            str(ncfg.get("continuity_frame_node_id") or "").strip(),
            str(ncfg.get("scene_prompt_handoff_from") or "").strip(),
            str(ncfg.get("scene_master_frame_id") or "").strip(),
        }
        return str(pred_id) in soft_ids and bool(str(pred_id))

    # Scene card → clip: always hard (R2V reference media). Prompt handoff alone is not enough.
    if nrole == "clip" and prole == "scene":
        return False

    return False


def artifact_dependency_satisfied(
    graph: DesignerExecutionGraph,
    node_id: str,
    pred_id: str,
) -> bool:
    """Whether the soft artifact this node needs from ``pred_id`` is already available."""
    by_id = {
        str(n.get("id") or ""): n
        for n in (graph.get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    }
    pred = by_id.get(str(pred_id or ""))
    node = by_id.get(str(node_id or ""))
    if pred is None or node is None:
        return False
    ncfg = node.get("config") if isinstance(node.get("config"), dict) else {}
    pcfg = pred.get("config") if isinstance(pred.get("config"), dict) else {}
    prole = _pipeline_role(pred)
    nrole = _pipeline_role(node)
    frame_roles = {"frame", "keyframe"}

    if nrole in {"clip"} and prole in {"clip"}:
        # Same-setting clip handoff needs the prior Wan prompt text — not only the
        # storyboard shot_action (that is known at graph build and would unlock too early).
        if str(ncfg.get("previous_clip_wan_prompt") or "").strip():
            return True
        if isinstance(ncfg.get("previous_clip_continuity_card"), dict):
            return True
        prior_prompt = str(
            pcfg.get("last_wan_prompt")
            or pcfg.get("last_approved_prompt")
            or pcfg.get("clip_prompt_preview")
            or ""
        ).strip()
        if prior_prompt and (
            bool(pcfg.get("handoff_artifact_ready"))
            or isinstance(pcfg.get("continuity_card"), dict)
        ):
            return True
        return False

    if (nrole in frame_roles and prole in frame_roles) or (
        nrole == "clip" and prole in frame_roles
    ):
        if isinstance(ncfg.get("previous_keyframe_continuity_card"), dict):
            return True
        if str(
            ncfg.get("scene_architecture_clause")
            or ncfg.get("scene_master_prompt")
            or ncfg.get("previous_keyframe_action")
            or ncfg.get("previous_keyframe_prompt")
            or ""
        ).strip():
            return True
        return bool(
            str(
                pcfg.get("scene_architecture_clause")
                or pcfg.get("scene_master_prompt")
                or pcfg.get("last_approved_prompt")
                or (pcfg.get("generate") or {}).get("prompt")
                or ""
            ).strip()
        ) and (
            bool(pcfg.get("handoff_artifact_ready"))
            or bool(
                str(
                    pcfg.get("last_approved_prompt")
                    or pcfg.get("scene_master_prompt")
                    or pcfg.get("scene_architecture_clause")
                    or ""
                ).strip()
            )
        )

    return False


def _state_published_media(run: dict[str, Any] | None, node_id: str) -> bool:
    """True when a running node has already stored the image/video a clip needs."""
    if not isinstance(run, dict):
        return False
    state = (run.get("node_states") or {}).get(node_id) or {}
    if not isinstance(state, dict):
        return False
    if str(state.get("status") or "") not in {"running", "completed"}:
        return False
    ref = state.get("output_ref")
    if not isinstance(ref, dict):
        return False
    uri = str(ref.get("uri") or "").strip()
    if not uri or uri.startswith("designer://"):
        return False
    kind = str(ref.get("kind") or "").strip().lower()
    mime = str(ref.get("mime_type") or "").strip().lower()
    if kind in {"text", "table"} or mime.startswith("text/"):
        return False
    return True


def filter_ready_by_dependency_order(
    ready_ids: list[str],
    *,
    preds: dict[str, list[str]],
    in_flight: set[str] | frozenset[str] | None = None,
    graph: DesignerExecutionGraph | None = None,
    run: dict[str, Any] | None = None,
) -> list[str]:
    """Start a node once its inputs exist, even if a dependency is still running.

    Another clip may start in the same batch when its storyboard shot is already
    known. A character or scene specs unlocks its clip once the image file is
    stored. The composer never joins while any dependency is still in flight.
    """
    if not ready_ids:
        return []
    by_id = {
        str(n.get("id") or ""): n
        for n in ((graph or {}).get("nodes") or [])
        if isinstance(n, dict) and n.get("id")
    } if graph else {}
    flying = set(in_flight or ())
    ordered = sorted(
        ready_ids,
        key=lambda nid: _node_schedule_priority(by_id[nid]) if nid in by_id else (50, 0, nid),
    )
    selected: list[str] = []
    selected_set: set[str] = set()
    for nid in ordered:
        blocked = False
        compose_sink = is_compose_sink_node(by_id.get(nid))
        for pred in preds.get(nid, []):
            if pred not in selected_set and pred not in flying:
                continue
            if compose_sink:
                blocked = True
                break
            soft = bool(
                graph is not None and is_soft_artifact_dependency(graph, pred, nid)
            )
            if soft and graph is not None and artifact_dependency_satisfied(graph, nid, pred):
                continue
            if _state_published_media(run, pred):
                continue
            blocked = True
            break
        if blocked:
            continue
        selected.append(nid)
        selected_set.add(nid)
    return selected


def sync_groups(graph: DesignerExecutionGraph) -> dict[str, frozenset[str]]:
    """Union-find over undirected ``sync`` edges."""
    parent: dict[str, str] = {node["id"]: node["id"] for node in graph.get("nodes", [])}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left: str, right: str) -> None:
        root_left, root_right = find(left), find(right)
        if root_left != root_right:
            parent[root_right] = root_left

    for edge in graph.get("edges", []):
        if edge_kind(edge) != EDGE_KIND_SYNC:
            continue
        source = edge.get("source")
        target = edge.get("target")
        if isinstance(source, str) and isinstance(target, str) and source in parent and target in parent:
            union(source, target)

    groups: dict[str, set[str]] = {}
    for node_id in parent:
        root = find(node_id)
        groups.setdefault(root, set()).add(node_id)
    return {node_id: frozenset(groups[find(node_id)]) for node_id in parent}


def build_bootstrap_graph(
    *,
    project_id: str,
    prompt: str,
    title: str | None = None,
) -> DesignerExecutionGraph:
    """Create the default video-creation pipeline graph from an initial prompt."""
    graph_id = new_graph_id()
    now = utc_now_ms()
    prompt_text = prompt.strip()
    graph_title = title.strip() if isinstance(title, str) and title.strip() else prompt_text[:80]
    nodes: list[DesignerGraphNode] = [
        {
            "id": "n_brief",
            "type": NODE_TYPE_TEXT,
            "label": "Text 1",
            "config": {
                "role": NODE_TYPE_TEXT,
                "pipeline": PIPELINE_BRIEF,
                "prompt": prompt_text,
            },
            "layout": {"x": 40, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_character",
            "type": NODE_TYPE_IMAGE,
            "label": "Image 1",
            "config": {
                "role": NODE_TYPE_IMAGE,
                "pipeline": PIPELINE_CHARACTER_DESIGN,
                "inputs": ["n_brief"],
            },
            "layout": {"x": 400, "y": 40, "width": 280, "height": 160},
        },
        {
            "id": "n_scene",
            "type": NODE_TYPE_IMAGE,
            "label": "Scene 1: Place",
            "config": {
                "role": NODE_TYPE_IMAGE,
                "pipeline": PIPELINE_SCENE,
                "inputs": ["n_brief", "n_storyboard"],
            },
            "layout": {"x": 760, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_storyboard",
            "type": NODE_TYPE_TABLE,
            "label": "Table 1",
            "config": {
                "role": NODE_TYPE_TABLE,
                "pipeline": PIPELINE_STORYBOARD,
                "inputs": ["n_brief"],
            },
            "layout": {"x": 400, "y": 440, "width": 280, "height": 160},
        },
        {
            "id": "n_clip_1",
            "type": NODE_TYPE_VIDEO,
            "label": "Video 1",
            "config": {
                "role": NODE_TYPE_VIDEO,
                "pipeline": PIPELINE_CLIP,
                "shot_index": 1,
                "inputs": ["n_character", "n_scene", "n_storyboard"],
            },
            "layout": {"x": 1120, "y": 240, "width": 280, "height": 160},
        },
        {
            "id": "n_compose",
            "type": NODE_TYPE_VIDEO,
            "label": "Video 2",
            "config": {
                "role": NODE_TYPE_VIDEO,
                "pipeline": PIPELINE_COMPOSE,
                "inputs": ["n_clip_1"],
            },
            "layout": {"x": 1480, "y": 240, "width": 280, "height": 160},
        },
    ]
    edges: list[DesignerGraphEdge] = [
        {"id": "e_brief_character", "source": "n_brief", "target": "n_character", "kind": EDGE_KIND_DATA},
        {"id": "e_brief_storyboard", "source": "n_brief", "target": "n_storyboard", "kind": EDGE_KIND_DATA},
        {"id": "e_brief_scene", "source": "n_brief", "target": "n_scene", "kind": EDGE_KIND_DATA},
        {"id": "e_storyboard_scene", "source": "n_storyboard", "target": "n_scene", "kind": EDGE_KIND_DATA},
        {
            "id": "e_character_storyboard",
            "source": "n_character",
            "target": "n_storyboard",
            "kind": EDGE_KIND_SYNC,
            "label": "Align",
        },
        {"id": "e_character_n_clip_1", "source": "n_character", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_scene_n_clip_1", "source": "n_scene", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_storyboard_n_clip_1", "source": "n_storyboard", "target": "n_clip_1", "kind": EDGE_KIND_DATA},
        {"id": "e_n_clip_1_compose", "source": "n_clip_1", "target": "n_compose", "kind": EDGE_KIND_DATA},
    ]
    graph: DesignerExecutionGraph = {
        "schema_version": SCHEMA_VERSION,
        "graph_id": graph_id,
        "project_id": project_id,
        "title": graph_title,
        "description": prompt_text,
        "source": GRAPH_SOURCE_PROMPT,
        "nodes": nodes,
        "edges": edges,
        "metadata": {
            "bootstrap": "designer.graph.bootstrap.v1",
            "scene_continuity_mode": "scene_card_plus_clip_shots",
        },
        "created_at": now,
        "updated_at": now,
    }
    return normalize_execution_graph(graph)
