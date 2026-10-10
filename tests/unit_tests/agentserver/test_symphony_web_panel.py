"""Skill Graph Web transport is routed through the shared SkillManager facade."""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface import _SKILL_ROUTES
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager


@dataclass
class _FakeSymphonyService:
    calls: list[tuple[str, object]] = field(default_factory=list)

    async def start_refresh_graph(self, *, force: bool = False) -> dict[str, object]:
        self.calls.append(("build", force))
        return {"success": True, "background": True, "build_status": "running"}

    async def cancel_build(self) -> dict[str, object]:
        self.calls.append(("cancel", None))
        return {"success": True, "cancelled": True, "build_status": "cancelled"}

    async def graph_status(self) -> dict[str, object]:
        self.calls.append(("status", None))
        return {"success": True, "build_status": "running"}

    async def graph(self) -> dict[str, object]:
        self.calls.append(("get", None))
        return {"success": True, "graph": {"nodes": [], "edges": []}}


@pytest.mark.parametrize(
    ("method", "handler_name"),
    [
        (ReqMethod.SKILLS_GRAPH_BUILD, "handle_skills_graph_build"),
        (ReqMethod.SKILLS_GRAPH_STATUS, "handle_skills_graph_status"),
        (ReqMethod.SKILLS_GRAPH_GET, "handle_skills_graph_get"),
        (ReqMethod.SKILLS_GRAPH_CANCEL, "handle_skills_graph_cancel"),
    ],
)
def test_skill_graph_methods_use_shared_skill_routes(method, handler_name):
    assert _SKILL_ROUTES[method] == handler_name


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("handler_name", "params", "expected_call"),
    [
        ("handle_skills_graph_build", {"force": "true"}, ("build", True)),
        ("handle_skills_graph_cancel", {}, ("cancel", None)),
        ("handle_skills_graph_status", {}, ("status", None)),
        ("handle_skills_graph_get", {}, ("get", None)),
    ],
)
async def test_skill_graph_handlers_delegate_to_process_service(
    monkeypatch,
    handler_name,
    params,
    expected_call,
):
    service = _FakeSymphonyService()
    monkeypatch.setattr(
        "jiuwenswarm.symphony.service.get_swarm_symphony_service",
        lambda: service,
    )
    manager = object.__new__(SkillManager)

    result = await getattr(manager, handler_name)(params)

    assert result["success"] is True
    assert service.calls == [expected_call]


def test_legacy_symphony_web_methods_are_not_request_methods():
    values = {method.value for method in ReqMethod}
    assert {
        "symphony.build_score",
        "symphony.pause_build",
        "symphony.score_status",
        "symphony.graph",
    }.isdisjoint(values)
