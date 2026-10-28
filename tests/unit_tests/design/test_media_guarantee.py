# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""MediaGuaranteeBackend failure messages for member nodes."""

from __future__ import annotations

from typing import Any

from openjiuwen.agent_teams.workflow.engine.backends.base import AgentResult
from openjiuwen.agent_teams.workflow.engine.errors import BackendError

from jiuwenswarm.server.runtime.design.event_bridge import DesignEventBridge
from jiuwenswarm.server.runtime.design.media_guarantee import MediaGuaranteeBackend

_NO_RECEIPT = "worker 'run-1-n-brief' did not submit a structured result via structured_output"
_SCHEMA = {"type": "object"}


class _RaisingInner:
    async def run(self, prompt, opts, schema_json, *, call_key=None) -> AgentResult:
        raise BackendError(_NO_RECEIPT)


class _ReturningInner:
    async def run(self, prompt, opts, schema_json, *, call_key=None) -> AgentResult:
        return AgentResult(text="done")


def _backend(inner: Any) -> tuple[MediaGuaranteeBackend, DesignEventBridge, dict[str, Any]]:
    graph = {
        "graph_id": "graph_1",
        "nodes": [{"id": "n_brief", "type": "text", "config": {"pipeline": "brief"}}],
    }
    run: dict[str, Any] = {"run_id": "run_1", "graph_id": "graph_1", "node_states": {}}
    bridge = DesignEventBridge(run, None, graph=graph)
    return MediaGuaranteeBackend(inner, graph=graph, run=run, bridge=bridge), bridge, run


async def test_member_failure_reports_the_recorded_generation_error():
    backend, bridge, run = _backend(_RaisingInner())
    bridge.note_member_error("n_brief", "brief generation failed: model timeout")

    result = await backend.run("prompt", {"label": "n_brief"}, _SCHEMA)

    assert result.skipped is True
    state = run["node_states"]["n_brief"]
    assert state["status"] == "failed"
    assert state["error"]
    assert "structured_output" not in state["error"]


async def test_member_failure_without_a_recorded_error_keeps_the_worker_error():
    backend, _bridge, run = _backend(_RaisingInner())

    await backend.run("prompt", {"label": "n_brief"}, _SCHEMA)

    assert run["node_states"]["n_brief"]["error"] == _NO_RECEIPT


async def test_member_turn_without_submit_reports_the_recorded_error():
    backend, bridge, run = _backend(_ReturningInner())
    bridge.note_member_error("n_brief", "submit_node_output rejected: not_generated")

    result = await backend.run("prompt", {"label": "n_brief"}, _SCHEMA)

    assert result.skipped is True
    assert run["node_states"]["n_brief"]["error"] == "submit_node_output rejected: not_generated"


async def test_member_turn_without_submit_falls_back_to_the_generic_error():
    backend, _bridge, run = _backend(_ReturningInner())

    await backend.run("prompt", {"label": "n_brief"}, _SCHEMA)

    assert run["node_states"]["n_brief"]["error"] == "node n_brief finished without submit_node_output"


def test_clearing_a_member_error_forgets_it():
    _, bridge, _run = _backend(_RaisingInner())
    bridge.note_member_error("n_brief", "brief generation failed: model timeout")

    bridge.note_member_error("n_brief", None)

    assert bridge.member_error_for("n_brief") is None
