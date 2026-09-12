# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RPC lifecycle tests for expert inventory -> graph -> team materialization."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

import jiuwenswarm.server.agent_ws_server as server_module
from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.expert.expert_graph_service import (
    ExpertGraphOpResult,
    ExpertGraphService,
)
from jiuwenswarm.server.runtime.expert.expert_store import (
    ChainExpertPackageSource,
    LocalDirExpertPackageSource,
)


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


def _make_expert(
    root: Path,
    expert_id: str,
    *,
    skill_name: str,
    inputs: list[dict] | None = None,
    outputs: list[dict] | None = None,
) -> Path:
    package = root / expert_id
    persona = package / "persona"
    skill = package / "skills" / skill_name
    persona.mkdir(parents=True)
    skill.mkdir(parents=True)
    (persona / "ROLE.md").write_text(f"# {expert_id}\n", encoding="utf-8")
    (skill / "SKILL.md").write_text(
        f"---\nname: {skill_name}\ndescription: test\n---\n",
        encoding="utf-8",
    )
    (package / "manifest.json").write_text(
        json.dumps(
            {
                "packageType": "agent_template",
                "agentCard": {
                    "id": expert_id,
                    "name": f"{expert_id} 专家",
                    "description": "用于 ExpertGraph RPC 测试",
                },
                "persona": {"dir": "persona"},
                "skills": [{"dir": f"skills/{skill_name}", "mode": "all"}],
                "metadata": {
                    "tags": ["内容增长"],
                    "quickPrompts": ["帮我完成一份可直接使用的内容方案"],
                    "deliverables": ["可打开的 HTML 成品"],
                    "collaboration": {
                        "inputs": inputs or [],
                        "outputs": outputs or [],
                        "reusable": True,
                    },
                },
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    return package


def _service(tmp_path: Path) -> tuple[ExpertGraphService, Path, Path]:
    source_root = tmp_path / "source"
    destination_root = tmp_path / "experts"
    _make_expert(
        source_root,
        "insight-expert",
        skill_name="data-insight",
        outputs=[
            {
                "id": "insight.json",
                "mediaType": "application/json",
                "schema": "insight.v1",
                "description": "结构化洞察",
            }
        ],
    )
    _make_expert(
        source_root,
        "content-expert",
        skill_name="content-design",
        inputs=[
            {
                "id": "insight.json",
                "mediaType": "application/json",
                "schema": "insight.v1",
                "description": "结构化洞察",
            }
        ],
        outputs=[
            {
                "id": "campaign.html",
                "mediaType": "text/html",
                "schema": "campaign.v1",
                "description": "可打开的内容方案",
                "primary": True,
                "visibility": "public",
            }
        ],
    )
    source = ChainExpertPackageSource(
        [
            LocalDirExpertPackageSource(experts_dir=destination_root),
            LocalDirExpertPackageSource(experts_dir=source_root),
        ]
    )
    return (
        ExpertGraphService(
            source_factory=lambda: source,
            destination_root=destination_root,
        ),
        source_root,
        destination_root,
    )


@pytest.mark.asyncio
async def test_rpc_lifecycle_builds_mines_and_materializes_beta3_team(
    tmp_path: Path,
) -> None:
    service, _, destination_root = _service(tmp_path)

    refreshed = await service.execute("experts.inventory.refresh", {})
    assert refreshed.ok is True
    assert refreshed.payload["success"] is True
    assert refreshed.payload["inventory"]["stats"]["expertCount"] == 2

    empty_graph = await service.execute("experts.graph.get", {})
    assert empty_graph.payload == {"success": True, "graph": None}

    built = await service.execute("experts.graph.build", {})
    graph = built.payload["graph"]
    assert built.ok is True
    assert graph["stats"]["nodeCount"] == 2
    assert any(edge["type"] == "can_feed" for edge in graph["edges"])

    mined = await service.execute(
        "experts.teams.mine",
        {
            "graph_id": graph["graphId"],
            "min_members": 2,
            "max_members": 2,
            "limit": 6,
        },
    )
    assert mined.ok is True
    assert mined.payload["success"] is True
    assert mined.payload["candidates"]
    candidate = mined.payload["candidates"][0]

    materialized = await service.execute(
        "experts.teams.materialize",
        {"graph_id": graph["graphId"], "candidate_id": candidate["id"]},
    )
    assert materialized.ok is True
    assert materialized.payload["success"] is True
    assert materialized.payload["source_refreshed"] is True
    assert materialized.payload["expert"]["type"] == "team"
    assert materialized.payload["inventory"]["stats"]["expertCount"] == 3
    assert materialized.payload["graph"]["stats"]["nodeCount"] == 3
    assert materialized.payload["graph"]["graphId"] != graph["graphId"]
    assert materialized.payload["candidates"]
    installed = next(
        item
        for item in materialized.payload["candidates"]
        if item["id"] == candidate["id"]
    )
    assert installed["status"] == "installed"
    current_graph = await service.execute("experts.graph.get", {})
    assert (
        current_graph.payload["graph"]["graphId"]
        == materialized.payload["graph"]["graphId"]
    )
    manifest = json.loads(
        (destination_root / candidate["id"] / "manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["package_type"] == "agent_group"
    assert manifest["metadata"]["graphId"] == graph["graphId"]


@pytest.mark.asyncio
async def test_materialize_rejects_member_that_changed_into_nested_team(
    tmp_path: Path,
) -> None:
    service, source_root, _ = _service(tmp_path)
    built = await service.execute("experts.graph.build", {})
    graph_id = built.payload["graph"]["graphId"]
    mined = await service.execute(
        "experts.teams.mine",
        {"graph_id": graph_id, "min_members": 2, "max_members": 2},
    )
    candidate = mined.payload["candidates"][0]

    # Simulate the source changing after mining.  Materialization must inspect
    # the freshly fetched package rather than trusting the cached node type.
    (source_root / "insight-expert" / "manifest.json").write_text(
        json.dumps(
            {
                "package_type": "agent_group",
                "name": "insight-expert",
                "agents": ["leader"],
            }
        ),
        encoding="utf-8",
    )
    result = await service.execute(
        "experts.teams.materialize",
        {"graph_id": graph_id, "candidate_id": candidate["id"]},
    )

    assert result.ok is False
    assert result.payload["success"] is False
    assert result.payload["code"] == "NESTED_TEAM_UNSUPPORTED"


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["collaboration", "package-content"])
async def test_materialize_rejects_member_changed_after_mining(
    tmp_path: Path,
    mutation: str,
) -> None:
    service, source_root, destination_root = _service(tmp_path)
    built = await service.execute("experts.graph.build", {})
    graph_id = built.payload["graph"]["graphId"]
    mined = await service.execute(
        "experts.teams.mine",
        {"graph_id": graph_id, "min_members": 2, "max_members": 2},
    )
    candidate = mined.payload["candidates"][0]
    member = source_root / "insight-expert"

    if mutation == "collaboration":
        manifest_path = member / "manifest.json"
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        manifest["metadata"]["collaboration"]["outputs"][0]["schema"] = "insight.v2"
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    else:
        (member / "persona" / "ROLE.md").write_text(
            "# changed after mining\n", encoding="utf-8"
        )

    result = await service.execute(
        "experts.teams.materialize",
        {"graph_id": graph_id, "candidate_id": candidate["id"]},
    )

    assert result.ok is False
    assert result.payload["code"] == "GRAPH_STALE"
    assert not (destination_root / candidate["id"]).exists()


@pytest.mark.asyncio
async def test_rpc_returns_stable_validation_codes(tmp_path: Path) -> None:
    service, _, _ = _service(tmp_path)

    not_built = await service.execute("experts.teams.mine", {})
    assert not_built.payload["code"] == "GRAPH_NOT_BUILT"

    bad_params = await service.execute("experts.graph.build", {"force": "yes"})
    assert bad_params.payload["code"] == "BAD_REQUEST"

    built = await service.execute("experts.graph.build", {})
    stale = await service.execute(
        "experts.teams.mine", {"graph_id": built.payload["graph"]["graphId"] + "-old"}
    )
    assert stale.payload["code"] == "GRAPH_STALE"


@pytest.fixture(autouse=True)
def _wire_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        server_module,
        "encode_agent_response_for_wire",
        lambda resp, response_id: {
            "response_id": response_id,
            "ok": resp.ok,
            "payload": resp.payload,
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "method",
    [
        ReqMethod.EXPERTS_INVENTORY_REFRESH,
        ReqMethod.EXPERTS_GRAPH_BUILD,
        ReqMethod.EXPERTS_GRAPH_GET,
        ReqMethod.EXPERTS_TEAMS_MINE,
        ReqMethod.EXPERTS_TEAMS_MATERIALIZE,
    ],
)
async def test_websocket_dispatches_all_expert_graph_methods_to_thin_service(
    method: ReqMethod,
) -> None:
    server = server_module.AgentWebSocketServer()
    calls: list[tuple[str, dict]] = []

    class _FakeGraphService:
        async def execute(
            self, requested_method: str, params: dict
        ) -> ExpertGraphOpResult:
            calls.append((requested_method, params))
            return ExpertGraphOpResult(
                ok=True,
                payload={"success": True, "method": requested_method},
            )

    server._expert_graph_service = _FakeGraphService()
    envelope = e2a_from_agent_fields(
        request_id=f"req-{method.name.lower()}",
        channel_id="web",
        req_method=method,
        params={"probe": method.value},
    )
    ws = FakeWebSocket()

    await server._handle_message(
        ws,
        json.dumps(envelope.to_dict(), ensure_ascii=False),
        asyncio.Lock(),
    )

    assert calls == [(method.value, {"probe": method.value})]
    assert ws.sent == [
        {
            "response_id": f"req-{method.name.lower()}",
            "ok": True,
            "payload": {"success": True, "method": method.value},
        }
    ]
