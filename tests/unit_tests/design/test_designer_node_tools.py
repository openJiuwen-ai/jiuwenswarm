# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The designer group's shared member tools (``tools/node_tools.py``)."""

from __future__ import annotations

import importlib.util
import logging
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace
from typing import Any

import pytest

import jiuwenswarm
from jiuwenswarm.server.runtime.design.context import get_design_context_registry
from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge

_GROUP = (
    Path(jiuwenswarm.__file__).parent
    / "resources"
    / "agent"
    / "workspace"
    / "plugins"
    / "agent_groups"
    / "designer"
)
_NODE_TOOLS = _GROUP / "tools" / "node_tools.py"
_BRIEF_TOOLS = _GROUP / "agents" / "brief" / "tools"
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


def test_rejected_generate_call_is_recorded_on_its_node(node_tools, run_env):
    node_tools.note_rejection(_ticket("n_brief"), "brief", "generate_brief", "missing_prompt")

    assert (
        run_env["bridge"].member_error_for("n_brief")
        == "generate_brief rejected: missing_prompt"
    )


async def test_rejection_keeps_the_generation_error(node_tools, run_env, monkeypatch):
    async def broken(env, node, injected, kind):
        raise RuntimeError("model timeout")

    monkeypatch.setattr(node_tools, "_run_handler", broken)
    await node_tools.generate_node_output(_ticket("n_brief"), member="brief", prompt="写大纲")

    node_tools.note_rejection(
        _ticket("n_brief"), "brief", "generate_brief", "generate_already_called"
    )

    assert run_env["bridge"].member_error_for("n_brief") == "brief generation failed: model timeout"


async def test_rejection_after_a_successful_generation_is_not_recorded(
    node_tools, run_env, monkeypatch
):
    async def working(env, node, injected, kind):
        return _generated(), injected["prompt"]

    monkeypatch.setattr(node_tools, "_run_handler", working)
    await node_tools.generate_node_output(_ticket("n_brief"), member="brief", prompt="写大纲")

    node_tools.note_rejection(
        _ticket("n_brief"), "brief", "generate_brief", "generate_already_called"
    )

    assert run_env["bridge"].member_error_for("n_brief") is None


def test_rejection_on_another_members_node_is_only_logged(node_tools, run_env, caplog):
    with caplog.at_level(logging.WARNING):
        node_tools.note_rejection(_ticket("n_character"), "brief", "generate_brief", "missing_prompt")

    assert run_env["bridge"].member_error_for("n_character") is None
    assert "generate_brief rejected: error=missing_prompt (ticket_member_mismatch)" in caplog.text


async def test_unresolvable_generate_ticket_is_logged(node_tools, run_env, caplog):
    stale = f"design:{_GRAPH_ID}:run_stale:n_brief"

    with caplog.at_level(logging.WARNING):
        result = await node_tools.generate_node_output(stale, member="brief", prompt="写大纲")

    assert result["error"] == "ticket_run_mismatch"
    assert "brief generate rejected: error=ticket_run_mismatch" in caplog.text


@pytest.fixture
def generate_brief(monkeypatch) -> Any:
    monkeypatch.syspath_prepend(str(_BRIEF_TOOLS))
    for name in ("support", "designer_node_tools"):
        monkeypatch.delitem(sys.modules, name, raising=False)
    spec = importlib.util.spec_from_file_location(
        "generate_brief_under_test", _BRIEF_TOOLS / "generate_brief.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    yield module.GenerateBrief()
    for name in ("support", "designer_node_tools"):
        sys.modules.pop(name, None)


async def test_generate_tool_records_a_missing_prompt(generate_brief, run_env):
    result = await generate_brief.invoke({"shot_ticket": _ticket("n_brief"), "prompt": " "})

    assert result["error"] == "missing_prompt"
    assert (
        run_env["bridge"].member_error_for("n_brief")
        == "generate_brief rejected: missing_prompt"
    )


async def test_generate_tool_records_a_guarded_second_call(generate_brief, run_env):
    result = await generate_brief.invoke(
        {
            "shot_ticket": _ticket("n_brief"),
            "prompt": "写大纲",
            "__node_guard_reject__": "generate_already_called",
        }
    )

    assert result["error"] == "generate_already_called"
    assert (
        run_env["bridge"].member_error_for("n_brief")
        == "generate_brief rejected: generate_already_called"
    )
