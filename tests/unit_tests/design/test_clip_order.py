# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Clip numbering after shots are inserted, and the video length notice."""

from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.common.schema.designer_graph import clip_order_problems
from jiuwenswarm.server.runtime.designer.handlers.clip import note_video_duration
from jiuwenswarm.server.runtime.designer.handlers.types import NodeExecutionContext
from jiuwenswarm.server.runtime.designer.node_agent import _graph_patch_summary


def _clip(node_id: str, index: int | None, previous: str | None = None) -> dict[str, Any]:
    config: dict[str, Any] = {"pipeline": "clip", "inputs": [previous] if previous else []}
    if index is not None:
        config["clip_index"] = index
    return {"id": node_id, "type": "video", "config": config}


def _graph(*clips: dict[str, Any]) -> dict[str, Any]:
    return {"graph_id": "g_clips", "nodes": list(clips), "edges": []}


def test_built_clip_chain_has_no_problems() -> None:
    graph = _graph(
        _clip("n_clip_1", 1),
        _clip("n_clip_2", 2, "n_clip_1"),
        _clip("n_clip_3", 3, "n_clip_2"),
    )

    assert clip_order_problems(graph) == []
    assert "clip_order_warning" not in _graph_patch_summary(graph)


def test_inserted_shot_without_renumbering_is_reported() -> None:
    graph = _graph(
        _clip("n_clip_1", 1),
        _clip("n_clip_beans", None, "n_clip_1"),
        _clip("n_clip_2", 2, "n_clip_beans"),
        _clip("n_clip_3", 3, "n_clip_2"),
    )

    problems = clip_order_problems(graph)

    assert "clip_index 1 is shared by n_clip_1, n_clip_beans" in problems
    assert "clip_index skips 4 (expected 1..4)" in problems
    assert "n_clip_beans follows n_clip_1 (clip_index 1) but has clip_index 1" in problems
    assert "n_clip_2 follows n_clip_beans (clip_index 1) but has clip_index 2" not in problems
    summary = _graph_patch_summary(graph)
    assert summary["nodes"] == ["n_clip_1", "n_clip_beans", "n_clip_2", "n_clip_3"]
    assert "clip_index 1 is shared by n_clip_1, n_clip_beans" in summary["clip_order_warning"]


def test_new_id_wired_mid_chain_keeps_its_suffix_number() -> None:
    graph = _graph(
        _clip("n_clip_1", None),
        _clip("n_clip_4", None, "n_clip_1"),
        _clip("n_clip_2", None, "n_clip_4"),
        _clip("n_clip_3", None, "n_clip_2"),
    )

    assert clip_order_problems(graph) == [
        "n_clip_4 follows n_clip_1 (clip_index 1) but has clip_index 4",
        "n_clip_2 follows n_clip_4 (clip_index 4) but has clip_index 2",
    ]


def test_clip_index_beyond_the_clip_count_is_named() -> None:
    graph = _graph(
        _clip("n_clip_1", 1),
        _clip("n_clip_2", 2),
        _clip("n_clip_x", 10),
    )

    assert clip_order_problems(graph) == [
        "clip_index skips 3 (expected 1..3)",
        "n_clip_x has clip_index 10 outside 1..3",
    ]


def test_renumbered_insert_has_no_problems() -> None:
    graph = _graph(
        _clip("n_clip_1", 1),
        _clip("n_clip_4", 2, "n_clip_1"),
        _clip("n_clip_2", 3, "n_clip_4"),
        _clip("n_clip_3", 4, "n_clip_2"),
    )

    assert clip_order_problems(graph) == []


def _ctx(node: dict[str, Any], lines: list[str]) -> NodeExecutionContext:
    graph = {"metadata": {}, "nodes": [node], "edges": []}
    return NodeExecutionContext(
        graph=graph,
        run_id="r",
        node_id=node["id"],
        run={},
        emit_activity=lambda kind, text, tool="": lines.append(text),
    )


@pytest.mark.parametrize(
    ("model", "requested", "generated", "expected"),
    [
        (
            "MiniMax-H3-Max",
            3,
            5,
            ["MiniMax-H3-Max makes 5-15s clips: generating 5s instead of the requested 3s"],
        ),
        ("wan3.0-video", 3, 3, []),
    ],
)
def test_video_length_notice_only_when_the_model_changes_it(
    monkeypatch: pytest.MonkeyPatch,
    model: str,
    requested: int,
    generated: int,
    expected: list[str],
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.pipeline.model_capacity.configured_video_model_id",
        lambda: model,
    )
    node = {"id": "v1", "type": "video", "config": {"duration_sec": requested}}
    lines: list[str] = []

    note_video_duration(_ctx(node, lines), node, generated)

    assert lines == expected


def test_no_length_notice_without_a_requested_length() -> None:
    node = {"id": "v1", "type": "video", "config": {}}
    lines: list[str] = []

    note_video_duration(_ctx(node, lines), node, 6)

    assert lines == []
