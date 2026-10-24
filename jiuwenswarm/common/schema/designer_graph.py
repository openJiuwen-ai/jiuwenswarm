# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Designer execution graph domain schema (canonical Python contract).

Shared between the React Flow frontend, gateway RPC handlers, and the graph
executor.  Frontend mirrors live in
``channels/web/frontend/src/features/designer/executionGraphTypes.ts``;
cross-layer literals are pinned by
``tests/unit_tests/design/test_designer_execution_graph_contract.py``.
"""

from __future__ import annotations

import re
import secrets
import time
from typing import Any, TypedDict

from jiuwenswarm.common.schema.designer_node_labels import (
    derive_clip_name,
    is_semantic_canvas_label,
    label_clip,
)

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
PIPELINE_CHARACTER = "character"
PIPELINE_SCENE = "scene"
PIPELINE_STORYBOARD = "storyboard"
PIPELINE_CLIP = "clip"
PIPELINE_COMPOSE = "compose"
PIPELINE_MUSIC = "music"
PIPELINE_SPEECH = "speech"

# Role aliases used by handlers / tests for pipeline identity.
NODE_ROLE_BRIEF = PIPELINE_BRIEF
NODE_ROLE_CHARACTER = PIPELINE_CHARACTER
NODE_ROLE_SCENE = PIPELINE_SCENE
NODE_ROLE_STORYBOARD = PIPELINE_STORYBOARD
NODE_ROLE_CLIP = PIPELINE_CLIP
NODE_ROLE_COMPOSE = PIPELINE_COMPOSE
NODE_ROLE_MUSIC = PIPELINE_MUSIC
NODE_ROLE_SPEECH = PIPELINE_SPEECH

PIPELINES: frozenset[str] = frozenset(
    {
        PIPELINE_BRIEF,
        PIPELINE_CHARACTER,
        PIPELINE_SCENE,
        PIPELINE_STORYBOARD,
        PIPELINE_CLIP,
        PIPELINE_COMPOSE,
        PIPELINE_MUSIC,
        PIPELINE_SPEECH,
    }
)

PIPELINE_TO_TYPE: dict[str, str] = {
    PIPELINE_BRIEF: NODE_TYPE_TEXT,
    PIPELINE_CHARACTER: NODE_TYPE_IMAGE,
    PIPELINE_SCENE: NODE_TYPE_IMAGE,
    PIPELINE_STORYBOARD: NODE_TYPE_TABLE,
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
    r"^(?:关键帧|视频片段|Clip)(?:\s*(\d+))?(?:首帧)?$",
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


def pipeline_node_label(role: str, clip_index: int = 1) -> str:
    """Canvas title for a role or pipeline id — always a modality name."""
    del clip_index
    modality = PIPELINE_TO_TYPE.get(role, role)
    return modality_node_label(modality)


def english_pipeline_label(label: str, role: str, clip_index: int = 1) -> str:
    """Rewrite known pipeline titles to modality names; leave semantic / custom names alone."""
    text = (label or "").strip()
    if not text:
        desired = pipeline_node_label(role, clip_index)
        return desired or text
    # Designer semantic scheme (Brief: … / Character N: … / Scene N: Clip …) must survive.
    if is_semantic_canvas_label(text):
        return text
    # Any label with a descriptive suffix after ":" is author/LLM-owned.
    if ":" in text and not _INDEXED_LABEL_RE.match(text):
        return text
    desired = pipeline_node_label(role, clip_index)
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
        return PIPELINE_CHARACTER
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

# Values are bare member names of the bound designer AgentGroup: the
# swarmflow engine's agent_template gate rejects "group/member" refs (the
# group is the session binding). Each pipeline runs as its same-named member.
# Music / speech have no member (no standalone audio nodes yet), and a text
# node without a pipeline has none either: the compiler rejects it.
ROLE_DEFAULT_TEMPLATES: dict[str, str] = {
    PIPELINE_BRIEF: "brief",
    PIPELINE_CHARACTER: "character",
    PIPELINE_SCENE: "scene",
    PIPELINE_STORYBOARD: "storyboard",
    PIPELINE_CLIP: "clip",
    PIPELINE_COMPOSE: "compose",
    NODE_TYPE_TABLE: "storyboard",
    NODE_TYPE_IMAGE: "character",
    NODE_TYPE_VIDEO: "clip",
}

# ── Edge kinds ────────────────────────────────────────────────────────────────

EDGE_KIND_DATA = "data"

EDGE_KINDS: frozenset[str] = frozenset({EDGE_KIND_DATA})

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
NODE_HISTORY_LIMIT = 20

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
    clip_index: int


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
    text: str
    candidate_text: str
    history: list[dict[str, Any]]
    # Content-iteration version of the node's output (starts at 1, +1 per
    # single-node rerun / cascade execution of the node). The compiler encodes
    # it into the engine ``phase`` ("v{n}") so a rerun misses the WAL cache and
    # executes live while untouched nodes replay. Store-side truth only.
    version: int


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


# Separators and relative segments that Windows, macOS, and Linux treat as
# part of a path. Other characters, including non-ASCII names, stay allowed.
_STORAGE_ID_PATH_CHARS = frozenset("/\\:\x00")
_STORAGE_ID_DOT_SEGMENTS = frozenset({".", ".."})


def storage_component(value: str) -> str | None:
    """One directory or file component, or None when ``value`` can escape a directory.

    ``/`` and ``\\`` are path separators. ``:`` is a Windows drive / alternate-data-stream
    marker and the classic Mac path separator. NUL terminates a path. ``.`` and ``..``
    are relative segments when the id is used as a directory name.
    """
    if not isinstance(value, str):
        return None
    text = value.strip()
    if not text or text in _STORAGE_ID_DOT_SEGMENTS:
        return None
    if any(char in _STORAGE_ID_PATH_CHARS for char in text):
        return None
    return text


def _require_storage_id(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise DesignerGraphValidationError(f"{field} must be a non-empty string")
    component = storage_component(value)
    if component is None:
        raise DesignerGraphValidationError(f"{field} must not contain path separators")
    return component


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
        value = agent_template.strip()
        # Legacy payloads carry "designer/<member>"; the engine takes bare names.
        group, sep, member = value.partition("/")
        if sep and group.strip() == DESIGNER_AGENT_GROUP_NAME and member.strip():
            value = member.strip()
        config[CONFIG_KEY_AGENT_TEMPLATE] = value
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
        raise DesignerGraphValidationError(
            f"unsupported node type: {node_type!r}. node.type must be one of "
            f"{sorted(NODE_TYPES)} (modality only). Narrative roles like "
            "outline/character/storyboard/script/scene are NOT node types — "
            "use type='text' (or the fitting modality) and put the role in "
            "config.pipeline / config.role."
        )
    label = raw.get("label")
    config = normalize_node_config(raw.get("config"))
    if not str(config.get(CONFIG_KEY_PIPELINE) or "").strip():
        inferred = infer_pipeline_from_id(node_id)
        if inferred:
            config[CONFIG_KEY_PIPELINE] = inferred
    if not str(config.get(CONFIG_KEY_ROLE) or "").strip():
        config[CONFIG_KEY_ROLE] = node_type
    role = str(config.get(CONFIG_KEY_ROLE) or node_type).strip()
    clip_raw = config.get("clip_index")
    try:
        clip_index = int(clip_raw) if clip_raw is not None else 1
    except (TypeError, ValueError):
        clip_index = 1
    current_label = str(label).strip() if isinstance(label, str) and label.strip() else node_id
    node: DesignerGraphNode = {
        "id": node_id,
        "type": node_type,
        "label": english_pipeline_label(current_label, role, clip_index),
        "config": config,
        "layout": normalize_layout(raw.get("layout")),
    }
    if "output_ref" in raw:
        node["output_ref"] = normalize_asset_ref(raw.get("output_ref"))
    return node


def normalize_edge(raw: Any) -> DesignerGraphEdge | None:
    """Normalize a data edge. Legacy ``sync`` edges are dropped (return None)."""
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("edge must be an object")
    edge_id = _require_str(raw.get("id"), "edge.id")
    source = _require_str(raw.get("source"), "edge.source")
    target = _require_str(raw.get("target"), "edge.target")
    kind_raw = raw.get("kind")
    kind = EDGE_KIND_DATA
    if isinstance(kind_raw, str) and kind_raw.strip():
        kind = kind_raw.strip()
    # Sync barriers are no longer created or honored — drop on load.
    if kind == "sync":
        return None
    if kind not in EDGE_KINDS:
        raise DesignerGraphValidationError(f"unsupported edge kind: {kind!r}")
    edge: DesignerGraphEdge = {
        "id": edge_id,
        "source": source,
        "target": target,
        "kind": EDGE_KIND_DATA,
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
    graph_id = _require_storage_id(raw.get("graph_id"), "graph_id")
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
    edges = []
    for item in raw_edges:
        edge = normalize_edge(item)
        if edge is not None:
            edges.append(edge)
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
    return stamp_concat_video_nodes(graph)


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
        for right in visible[index + 1:]
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


def _is_clip_node(node: dict[str, Any]) -> bool:
    return node_pipeline(node) == PIPELINE_CLIP or str(node.get("id") or "").startswith("n_clip")


def repair_overlapping_pipeline_layout(graph: DesignerExecutionGraph) -> DesignerExecutionGraph:
    """Push 成片 to the right of clips and unstack overlapping bootstrap columns."""
    nodes = [node for node in (graph.get("nodes") or []) if isinstance(node, dict)]
    by_id = {str(node.get("id") or ""): node for node in nodes if node.get("id")}
    _restack_column(
        [by_id[key] for key in ("n_character", "n_scene", "n_storyboard") if key in by_id]
    )
    clips = sorted(
        [node for node in nodes if _is_clip_node(node)],
        key=node_clip_index,
    )
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


def clip_node_id(clip_index: int) -> str:
    return f"n_clip_{int(clip_index)}"


def node_clip_index(node: DesignerGraphNode) -> int:
    raw = node_config(node).get("clip_index")
    if isinstance(raw, bool):
        raw = None
    if isinstance(raw, int) and raw > 0:
        return raw
    if isinstance(raw, str) and raw.strip().isdigit():
        value = int(raw.strip())
        if value > 0:
            return value
    node_id = str(node.get("id") or "")
    if node_id == "n_clip":
        return 1
    if node_id.startswith("n_clip_"):
        suffix = node_id.rsplit("_", 1)[-1]
        if suffix.isdigit() and int(suffix) > 0:
            return int(suffix)
    return 1


def clip_order_problems(graph: DesignerExecutionGraph) -> list[str]:
    """Shot numbering that would pair clips with the wrong storyboard rows.

    A clip reads its storyboard row, and compose orders the film, by
    ``node_clip_index``. Clips must therefore be numbered 1..N without repeats,
    and a clip wired after another clip must carry the next number.
    """
    index_by_id = {
        str(node.get("id") or ""): node_clip_index(node)
        for node in graph.get("nodes") or []
        if isinstance(node, dict) and node_pipeline(node) == PIPELINE_CLIP
    }
    problems: list[str] = []
    ids_by_index: dict[int, list[str]] = {}
    for node_id, index in index_by_id.items():
        ids_by_index.setdefault(index, []).append(node_id)
    for index, node_ids in sorted(ids_by_index.items()):
        if len(node_ids) > 1:
            problems.append(f"clip_index {index} is shared by {', '.join(node_ids)}")
    missing = sorted(set(range(1, len(index_by_id) + 1)) - set(ids_by_index))
    if missing:
        problems.append(
            f"clip_index skips {', '.join(map(str, missing))} "
            f"(expected 1..{len(index_by_id)})"
        )
    for node_id, index in index_by_id.items():
        if not 1 <= index <= len(index_by_id):
            problems.append(f"{node_id} has clip_index {index} outside 1..{len(index_by_id)}")
    for node_id, index in index_by_id.items():
        previous = [
            source for source in video_concat_source_ids(graph, node_id) if source in index_by_id
        ]
        if len(previous) == 1 and index != index_by_id[previous[0]] + 1:
            problems.append(
                f"{node_id} follows {previous[0]} (clip_index {index_by_id[previous[0]]}) "
                f"but has clip_index {index}"
            )
    return problems


def _copied_delegate(graph: DesignerExecutionGraph) -> str | None:
    for node in graph.get("nodes") or []:
        delegate = str(node_config(node).get("delegate") or "").strip()
        if delegate:
            return delegate
    return None


def _is_clip_pipeline_id(node_id: str) -> bool:
    return (
        node_id in {"n_clip", COMPOSE_NODE_ID}
        or node_id.startswith("n_clip_")
    )


def _is_clip_pipeline_node(node: dict[str, Any]) -> bool:
    if node_pipeline(node) in {PIPELINE_CLIP, PIPELINE_COMPOSE}:
        return True
    return _is_clip_pipeline_id(str(node.get("id") or ""))


def _is_clip_pipeline_edge(edge: dict[str, Any]) -> bool:
    if _is_clip_pipeline_id(str(edge.get("source") or "")):
        return True
    return _is_clip_pipeline_id(str(edge.get("target") or ""))


_BOOTSTRAP_CLIP_IDS = frozenset({"n_clip", "n_clip_1"})


def _clip_pipeline_ids_set(graph: DesignerExecutionGraph) -> set[str]:
    clips: set[str] = set()
    for node in graph.get("nodes") or []:
        node_id = str(node.get("id") or "")
        if not node_id:
            continue
        pipeline = node_pipeline(node)
        if pipeline == PIPELINE_CLIP or node_id == "n_clip" or node_id.startswith("n_clip_"):
            clips.add(node_id)
    return clips


def shot_pipeline_count(graph: DesignerExecutionGraph) -> int:
    """How many per-clip slots the graph currently has."""
    clips = _clip_pipeline_ids_set(graph)
    return max(len(clips), 0)


def is_one_shot_bootstrap_skeleton(graph: DesignerExecutionGraph) -> bool:
    """True when the graph still looks like the 1-clip bootstrap, not a user edit."""
    clips = _clip_pipeline_ids_set(graph)
    if not clips:
        return False
    if any(node_id not in _BOOTSTRAP_CLIP_IDS for node_id in clips):
        return False
    return len(clips) <= 1


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
    """Keep already-expanded clip nodes when a stale save sends the bootstrap shape.

    User edits that drop extra clips (Video 6 / Video 4) must persist. Only a
    1-clip bootstrap skeleton is grafted back onto an expanded graph.
    """
    if existing is None:
        return incoming
    incoming_meta = incoming.get("metadata") if isinstance(incoming.get("metadata"), dict) else {}
    if incoming_meta.get("user_topology_edit"):
        return incoming
    existing_count = shot_pipeline_count(existing)
    incoming_count = shot_pipeline_count(incoming)
    is_bootstrap_skeleton = is_one_shot_bootstrap_skeleton(incoming) and incoming_count <= 1
    if not is_bootstrap_skeleton or existing_count <= max(incoming_count, 1):
        return incoming
    incoming_ids = {str(node.get("id") or "") for node in incoming.get("nodes") or []}
    grafted = dict(incoming)
    extra_nodes = [
        dict(node)
        for node in existing.get("nodes") or []
        if str(node.get("id") or "") not in incoming_ids and _is_clip_pipeline_node(node)
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
    """One clip node per storyboard clip (scene-card R2V), plus compose."""
    count = max(1, int(shot_count or 1))
    raw = dict(graph)
    existing_by_id = {
        str(node.get("id") or ""): dict(node)
        for node in raw.get("nodes") or []
        if str(node.get("id") or "")
    }
    clip_template = existing_by_id.get("n_clip_1") or existing_by_id.get("n_clip") or {}
    clip_base = dict(clip_template.get("layout") or {})

    kept_nodes = [dict(node) for node in raw.get("nodes") or [] if not _is_clip_pipeline_node(node)]
    kept_edges = [dict(edge) for edge in raw.get("edges") or [] if not _is_clip_pipeline_edge(edge)]
    kept_ids = {str(node.get("id") or "") for node in kept_nodes}

    def _role_node_ids(role: str) -> list[str]:
        ids: list[str] = []
        for node in kept_nodes:
            if node_pipeline(node) == role and str(node.get("id") or ""):
                ids.append(str(node["id"]))
        return ids

    character_ids = _role_node_ids(NODE_ROLE_CHARACTER)
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
            "clip_index": index,
            "clip_strategy": "clip_from_scene_and_character_specs",
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
        clip_name = derive_clip_name(
            {"title": f"Clip {index}", "action": ""},
            fallback_index=index,
        )
        clip_nodes.append(
            {
                "id": clip_id,
                "type": NODE_TYPE_VIDEO,
                "label": label_clip(
                    scene_number=1, clip_number=index, clip_name=clip_name
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
    if activity is None:
        raise DesignerGraphValidationError("node activity must not be empty")
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
    version = raw.get("version")
    if version is not None:
        if isinstance(version, int) and not isinstance(version, bool) and version > 0:
            state["version"] = version
        else:
            raise DesignerGraphValidationError("node_state.version must be a positive integer")
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
    # team.design execution state: the node's output text snapshot (feeds
    # compile-time upstream inlining), the single-node rerun candidate text,
    # and the per-node rerun history (one entry per trigger).
    for text_key in ("text", "candidate_text"):
        text_val = raw.get(text_key)
        if text_val is not None:
            if not isinstance(text_val, str):
                raise DesignerGraphValidationError(f"node_state.{text_key} must be a string")
            state[text_key] = text_val  # type: ignore[literal-required]
    history = raw.get("history")
    if history is not None:
        if not isinstance(history, list):
            raise DesignerGraphValidationError("node_state.history must be an array")
        entries: list[dict[str, Any]] = []
        for entry in history:
            if not isinstance(entry, dict):
                continue
            normalized_entry: dict[str, Any] = {
                "ts": entry.get("ts") if isinstance(entry.get("ts"), int) else utc_now_ms(),
                "text": str(entry.get("text") or ""),
            }
            ref = normalize_asset_ref(entry.get("output_ref"))
            if ref is not None:
                normalized_entry["output_ref"] = ref
            entries.append(normalized_entry)
        state["history"] = entries[-NODE_HISTORY_LIMIT:]
    return state


def normalize_execution_run(raw: Any) -> DesignerExecutionRun:
    if not isinstance(raw, dict):
        raise DesignerGraphValidationError("run must be an object")
    schema_version = _require_str(raw.get("schema_version"), "schema_version")
    if schema_version != RUN_SCHEMA_VERSION:
        raise DesignerGraphValidationError(
            f"unsupported run schema_version: {schema_version!r} "
            f"(expected {RUN_SCHEMA_VERSION!r})"
        )
    run_id = _require_storage_id(raw.get("run_id"), "run_id")
    graph_id = _require_storage_id(raw.get("graph_id"), "graph_id")
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
    """Resolve the AgentTemplate member name for a node (explicit, else pipeline / type default).

    The value is a bare member name; a legacy ``designer/`` prefix on raw
    graph payloads is stripped here too.
    """
    explicit = str(node_config(node).get(CONFIG_KEY_AGENT_TEMPLATE) or "").strip()
    if explicit.startswith(f"{DESIGNER_AGENT_GROUP_NAME}/"):
        explicit = explicit.partition("/")[2].strip()
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


def _merge_node_patch(existing: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    """Preserve omitted object fields; explicit scalars and lists replace them."""
    merged = dict(existing)
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_node_patch(merged[key], value)
        else:
            merged[key] = value
    return merged


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
        if isinstance(item, dict):
            existing = nodes_by_id.get(str(item.get("id") or "").strip())
            if existing is not None:
                merged = {**existing, **item}
                for field in ("config", "layout"):
                    if isinstance(existing.get(field), dict) and isinstance(item.get(field), dict):
                        merged[field] = _merge_node_patch(existing[field], item[field])
                item = merged
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
        if edge is None:
            continue
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
    # Stable: clips by clip_index, then audio ids.

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
            clip = int(cfg.get("clip_index") or 0)
        except (TypeError, ValueError):
            clip = 0
        kind = 0 if role in {"clip"} or nid.startswith("n_clip") else 1
        return (kind, clip, nid)

    return sorted(out, key=_sort_key)


