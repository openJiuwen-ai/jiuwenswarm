# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The designer group's shared member tools (``tools/node_tools.py``)."""

from __future__ import annotations

import importlib.util
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import jiuwenswarm
from jiuwenswarm.server.runtime.design.context import get_design_context_registry
from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge

_NODE_TOOLS = (
    Path(jiuwenswarm.__file__).parent
    / "resources"
    / "agent"
    / "workspace"
    / "plugins"
    / "agent_groups"
    / "designer"
    / "tools"
    / "node_tools.py"
)
_SESSION = "session_node_tools_test"
_GRAPH_ID = "graph_node_tools_test"
_RUN_ID = "run_node_tools_test"


def _ticket(node_id: str) -> str:
    return f"design:{_GRAPH_ID}:{_RUN_ID}:{node_id}"


def _generated() -> SimpleNamespace:
    ref = {"kind": "text", "uri": "file:///C:/tmp/brief.md"}
    return SimpleNamespace(output_ref=ref, output_refs=[ref], message="brief written")


@pytest.fixture(scope="module")
def node_tools() -> ModuleType:
    spec = importlib.util.spec_from_file_location("designer_node_tools_under_test", _NODE_TOOLS)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def run_env() -> Any:
    graph = {
        "graph_id": _GRAPH_ID,
        "nodes": [
            {"id": "n_brief", "type": "text", "config": {"pipeline": "brief"}},
            {"id": "n_character", "type": "image", "config": {"pipeline": "character"}},
        ],
    }
    run: dict[str, Any] = {"run_id": _RUN_ID, "graph_id": _GRAPH_ID, "node_states": {}}
    env = {
        "graph": graph,
        "run": run,
        "bridge": DesignEventBridge(run, None, graph=graph),
        "media_root": None,
        "generations": {},
    }
    registry = get_design_context_registry()
    registry.bind(_SESSION, _GRAPH_ID).node_run_env = env
    yield env
    registry.drop(_SESSION)


async def test_text_generation_retries_once_after_a_failure(node_tools, run_env, monkeypatch):
    calls: list[str] = []

    async def flaky(env, node, injected, kind):
        calls.append(kind)
        if len(calls) == 1:
            raise RuntimeError("Chat model did not return a usable brief.")
        return _generated(), injected["prompt"]

    monkeypatch.setattr(node_tools, "_run_handler", flaky)

    result = await node_tools.generate_node_output(_ticket("n_brief"), member="brief", prompt="写大纲")

    assert result["success"] is True
    assert calls == ["text", "text"]
    assert run_env["bridge"].member_error_for("n_brief") is None


async def test_text_generation_records_the_error_after_the_retry_fails(
    node_tools, run_env, monkeypatch
):
    async def broken(env, node, injected, kind):
        raise RuntimeError("model timeout")

    monkeypatch.setattr(node_tools, "_run_handler", broken)

    result = await node_tools.generate_node_output(_ticket("n_brief"), member="brief", prompt="写大纲")

    assert result["success"] is False
    assert result["error"] == "generation_failed: model timeout"
    assert run_env["bridge"].member_error_for("n_brief") == "brief generation failed: model timeout"


async def test_media_generation_is_not_retried(node_tools, run_env, monkeypatch):
    calls: list[str] = []

    async def broken(env, node, injected, kind):
        calls.append(kind)
        raise RuntimeError("image backend unavailable")

    monkeypatch.setattr(node_tools, "_run_handler", broken)

    result = await node_tools.generate_node_output(
        _ticket("n_character"), member="character", prompt="角色设定"
    )

    assert result["success"] is False
    assert calls == ["image"]


async def test_rejected_submit_keeps_the_generation_error(node_tools, run_env, monkeypatch):
    async def broken(env, node, injected, kind):
        raise RuntimeError("model timeout")

    monkeypatch.setattr(node_tools, "_run_handler", broken)
    await node_tools.generate_node_output(_ticket("n_brief"), member="brief", prompt="写大纲")

    result = await node_tools.submit_node_output(_ticket("n_brief"))

    assert result["error"] == "not_generated"
    assert run_env["bridge"].member_error_for("n_brief") == "brief generation failed: model timeout"


async def test_rejected_submit_is_recorded_when_nothing_failed_before(node_tools, run_env):
    result = await node_tools.submit_node_output(_ticket("n_brief"))

    assert result["error"] == "not_generated"
    assert (
        run_env["bridge"].member_error_for("n_brief")
        == "submit_node_output rejected: not_generated"
    )
