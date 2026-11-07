# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Cross-layer contract tests for Designer execution graph literals."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from jiuwenswarm.common.schema.designer_graph import (
    CONFIG_DELEGATES,
    CONFIG_DELEGATE_HANDLER,
    CONFIG_DELEGATE_SUBAGENT,
    CONFIG_DELEGATE_AGENT,
    CONFIG_KEYS,
    DESIGNER_AGENT_GROUP_NAME,
    LEADER_NODE_ID,
    ACTIVITY_KIND_THINKING,
    ACTIVITY_KIND_TOOL_CALL,
    ACTIVITY_KIND_STAGE,
    ROLE_DEFAULT_TEMPLATES,
    EDGE_KIND_DATA,
    EDGE_KINDS,
    NODE_ROLE_BRIEF,
    NODE_ROLE_CHARACTER,
    NODE_ROLE_CLIP,
    NODE_ROLE_COMPOSE,
    NODE_ROLE_SCENE,
    NODE_ROLE_STORYBOARD,
    NODE_ROLE_IMAGE,
    NODE_ROLE_VIDEO,
    NODE_ROLE_AUDIO,
    NODE_ROLES,
    NODE_TYPES,
    NODE_TYPE_AUDIO,
    NODE_TYPE_IMAGE,
    NODE_TYPE_TABLE,
    NODE_TYPE_TEXT,
    NODE_TYPE_VIDEO,
    RUN_SCHEMA_VERSION,
    SCHEMA_VERSION,
    node_pipeline,
    normalize_node,
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
_TS = (
    _REPO_ROOT
    / "jiuwenswarm"
    / "channels"
    / "web"
    / "frontend"
    / "src"
    / "features"
    / "designer"
    / "executionGraphTypes.ts"
)


def _extract_ts_const(path: Path, name: str) -> str:
    text = path.read_text(encoding="utf-8")
    pattern = rf"export const {re.escape(name)}\s*=\s*['\"]([^'\"]+)['\"]"
    match = re.search(pattern, text)
    assert match is not None, f"{name} not found in {path}"
    return match.group(1)


def _extract_ts_const_array(path: Path, name: str) -> list[str]:
    text = path.read_text(encoding="utf-8")
    start = text.find(f"export const {name}")
    assert start != -1, f"{name} array not found in {path}"
    bracket_start = text.find("[", start)
    bracket_end = text.find("] as const;", bracket_start)
    assert bracket_start != -1 and bracket_end != -1, f"{name} array body not found in {path}"
    body = text[bracket_start:bracket_end]
    literals = re.findall(r"['\"]([^'\"]+)['\"]", body)
    if literals:
        return literals
    refs = re.findall(r"DESIGNER_[A-Z0-9_]+", body)
    return [_extract_ts_const(path, ref) for ref in refs]


@pytest.mark.parametrize(
    ("python_const", "ts_const"),
    [
        (SCHEMA_VERSION, "DESIGNER_GRAPH_SCHEMA_VERSION"),
        (RUN_SCHEMA_VERSION, "DESIGNER_RUN_SCHEMA_VERSION"),
        (NODE_TYPE_TEXT, "DESIGNER_NODE_TYPE_TEXT"),
        (NODE_TYPE_TABLE, "DESIGNER_NODE_TYPE_TABLE"),
        (NODE_TYPE_IMAGE, "DESIGNER_NODE_TYPE_IMAGE"),
        (NODE_TYPE_VIDEO, "DESIGNER_NODE_TYPE_VIDEO"),
        (NODE_TYPE_AUDIO, "DESIGNER_NODE_TYPE_AUDIO"),
        (NODE_ROLE_BRIEF, "DESIGNER_NODE_ROLE_BRIEF"),
        (NODE_ROLE_CHARACTER, "DESIGNER_NODE_ROLE_CHARACTER"),
        (NODE_ROLE_SCENE, "DESIGNER_NODE_ROLE_SCENE"),
        (NODE_ROLE_STORYBOARD, "DESIGNER_NODE_ROLE_STORYBOARD"),
        # (NODE_ROLE_CLIP, "DESIGNER_NODE_ROLE_FRAME"),  # frame removed,
        (NODE_ROLE_CLIP, "DESIGNER_NODE_ROLE_CLIP"),
        (NODE_ROLE_COMPOSE, "DESIGNER_NODE_ROLE_COMPOSE"),
        (NODE_ROLE_IMAGE, "DESIGNER_NODE_ROLE_IMAGE"),
        (NODE_ROLE_VIDEO, "DESIGNER_NODE_ROLE_VIDEO"),
        (NODE_ROLE_AUDIO, "DESIGNER_NODE_ROLE_AUDIO"),
        (EDGE_KIND_DATA, "DESIGNER_EDGE_KIND_DATA"),
        (CONFIG_DELEGATE_HANDLER, "DESIGNER_CONFIG_DELEGATE_HANDLER"),
        (CONFIG_DELEGATE_SUBAGENT, "DESIGNER_CONFIG_DELEGATE_SUBAGENT"),
        (CONFIG_DELEGATE_AGENT, "DESIGNER_CONFIG_DELEGATE_AGENT"),
        (DESIGNER_AGENT_GROUP_NAME, "DESIGNER_AGENT_GROUP_NAME"),
        (LEADER_NODE_ID, "DESIGNER_LEADER_NODE_ID"),
        (ACTIVITY_KIND_THINKING, "DESIGNER_ACTIVITY_KIND_THINKING"),
        (ACTIVITY_KIND_TOOL_CALL, "DESIGNER_ACTIVITY_KIND_TOOL_CALL"),
        (ACTIVITY_KIND_STAGE, "DESIGNER_ACTIVITY_KIND_STAGE"),
    ],
)
def test_designer_literal_contract(python_const: str, ts_const: str) -> None:
    assert _extract_ts_const(_TS, ts_const) == python_const


def test_designer_node_types_contract() -> None:
    ts_types = set(_extract_ts_const_array(_TS, "DESIGNER_NODE_TYPES"))
    assert ts_types == set(NODE_TYPES)
    assert set(_extract_ts_const_array(_TS, "DESIGNER_NODE_ROLES")) == set(NODE_ROLES)
    assert set(_extract_ts_const_array(_TS, "DESIGNER_EDGE_KINDS")) == set(EDGE_KINDS)
    assert set(_extract_ts_const_array(_TS, "DESIGNER_CONFIG_DELEGATES")) == set(CONFIG_DELEGATES)
    assert set(_extract_ts_const_array(_TS, "DESIGNER_NODE_CONFIG_KEYS")) == set(CONFIG_KEYS)


def test_designer_role_default_templates_contract() -> None:
    text = _TS.read_text(encoding="utf-8")
    start = text.find("export const DESIGNER_ROLE_DEFAULT_TEMPLATES")
    assert start != -1
    brace_start = text.find("{", start)
    brace_end = text.find("} as const;", brace_start)
    body = text[brace_start : brace_end + 1]
    for role, ref in ROLE_DEFAULT_TEMPLATES.items():
        assert ref in body
        assert role.replace("_", "").lower() in body.replace("_", "").lower() or ref in body


def test_manual_modality_roles_normalize() -> None:
    image = normalize_node(
        {
            "id": "n_image_1",
            "type": NODE_TYPE_IMAGE,
            "label": "Image 1",
            "config": {"role": NODE_ROLE_IMAGE, "delegate": "handler"},
            "layout": {"x": 0, "y": 0, "width": 280, "height": 160},
        }
    )
    assert image["type"] == "image"
    assert image["config"]["role"] == "image"
    assert not image["config"].get("pipeline")


def test_legacy_pipeline_role_maps_to_modality() -> None:
    node = normalize_node(
        {
            "id": "n_character",
            "type": NODE_TYPE_IMAGE,
            "label": "Character",
            "config": {"role": NODE_ROLE_CHARACTER},
            "layout": {"x": 0, "y": 0, "width": 280, "height": 160},
        }
    )
    assert node["type"] == "image"
    assert node["config"]["role"] == "image"
    assert node["config"]["pipeline"] == NODE_ROLE_CHARACTER
    assert node["label"] == "Image"
    assert node_pipeline(node) == NODE_ROLE_CHARACTER
