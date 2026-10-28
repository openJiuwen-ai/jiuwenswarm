from __future__ import annotations

from typing import Any

import pytest

from jiuwenswarm.agents.harness.design.rails.designer_authoring_rail import (
    AUTORUN_FAILURE_HINT,
    DesignerAuthoringRail,
)
from jiuwenswarm.server.runtime.design.director_chain import DirectorChainError
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError


class _FakeStore:
    def get_graph(self, graph_id: str) -> dict[str, Any]:
        return {"graph_id": graph_id, "nodes": [], "edges": [], "metadata": {}}


def _rail(monkeypatch: pytest.MonkeyPatch) -> DesignerAuthoringRail:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.graph_store.DesignerGraphStore",
        _FakeStore,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm",
        lambda: None,
    )
    rail = DesignerAuthoringRail(session_id="sess_authoring_test")
    rail._graph_id = "g_authoring_test"
    return rail


@pytest.mark.asyncio
async def test_chain_failure_tells_leader_not_to_hand_build(monkeypatch):
    async def fail(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        raise DirectorChainError("模型未返回可用的角色/镜头分析")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.design.director_chain.run_director_chain",
        fail,
    )
    tool = _rail(monkeypatch)._build_tool()

    result = await tool.invoke({"prompt": "给这段音乐配一个 5 秒的视频"})

    assert result.startswith("error: 模型未返回可用的角色/镜头分析")
    assert result.endswith(AUTORUN_FAILURE_HINT)


@pytest.mark.asyncio
async def test_missing_model_tells_leader_not_to_hand_build(monkeypatch):
    rail = _rail(monkeypatch)

    def no_model() -> None:
        raise DesignerLlmError("未配置对话模型")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.designer.model_tools.require_llm",
        no_model,
    )
    tool = rail._build_tool()

    result = await tool.invoke({"prompt": "做一个短片"})

    assert result == f"error: 未配置对话模型\n{AUTORUN_FAILURE_HINT}"
