# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""专家团（team 包）入口与生命周期测试。

覆盖：ExpertService 团分支分派（load 幂等/换绑先卸后装/BUSY 并集/pending/
applied/unload 清字段）、_replay_expert_from_metadata 团跳过（防污染）、
chat 非 team mode 已绑团的 BAD_REQUEST 防御。
"""

import asyncio
import json
import shutil
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import jiuwenswarm.server.agent_ws_server as server_module
import jiuwenswarm.server.runtime.session.session_metadata as sm
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.expert import expert_store as es

TESTDATA_GROUP = (
        Path(__file__).parent / "testdata" / "expert_groups" / "sample-expert-group"
)
GROUP_ID = TESTDATA_GROUP.name


class FakeWebSocket:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    async def send(self, payload: str) -> None:
        self.sent.append(json.loads(payload))


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


@pytest.fixture
def server() -> server_module.AgentWebSocketServer:
    return server_module.AgentWebSocketServer()


def _request(method: ReqMethod, params: dict) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="desktop",
        req_method=method,
        params=params,
    )


@pytest.fixture
def local_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "experts"
    root.mkdir()
    shutil.copytree(TESTDATA_GROUP, root / GROUP_ID)
    source = es.LocalDirExpertPackageSource(experts_dir=root)
    monkeypatch.setattr(es, "get_expert_source", lambda: source)
    return root


@pytest.fixture
def metadata_store(monkeypatch: pytest.MonkeyPatch) -> dict:
    """内存版 session metadata（全字段写入）。"""
    store: dict[str, dict] = {}
    writes: list[dict] = []
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: dict(store.get(session_id, {})),
    )

    def _update(*, session_id: str, **kwargs):
        writes.append({"session_id": session_id, **kwargs})
        store.setdefault(session_id, {"session_id": session_id})
        for key, value in kwargs.items():
            if value is not None:
                store[session_id][key] = value

    monkeypatch.setattr(sm, "update_session_metadata", _update)
    store["__writes__"] = writes
    return store


@pytest.fixture
def team_runtime(monkeypatch: pytest.MonkeyPatch) -> SimpleNamespace:
    """team 运行时 fake：回合信号 + 停运行时记录。"""
    import jiuwenswarm.agents.harness.team.team_manager as tm_mod

    state = SimpleNamespace(
        stream_active=False,
        stop_calls=[],
        manager=SimpleNamespace(has_stream_task=lambda sid: state.stream_active),
    )

    async def _stop(session_id: str, reason: str = "", *, stop_runner: bool = True):
        state.stop_calls.append({"session_id": session_id, "reason": reason})
        return True

    monkeypatch.setattr(tm_mod, "get_team_manager", lambda: state.manager)
    monkeypatch.setattr(tm_mod, "stop_team_session_runtime_across_managers", _stop)
    return state


def _install_fake_adapters(
        monkeypatch: pytest.MonkeyPatch,
        server: server_module.AgentWebSocketServer,
        root,
) -> None:
    monkeypatch.setattr(
        server._agent_manager, "get_agent_nowait", lambda channel_id, **_: object()
    )
    monkeypatch.setattr(server, "_resolve_adapter", lambda agent: root)


def _live_root(child) -> SimpleNamespace:
    return SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: False,
    )


@pytest.mark.asyncio
async def test_load_team_pending_writes_full_binding(
        server, local_source: Path, metadata_store: dict, team_runtime
) -> None:
    """无活会话：pending=true，metadata 五字段写全。"""
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "", "expert_type": "agent"}
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["type"] == "team"
    assert msg["payload"]["pending"] is True
    assert msg["payload"]["applied"] is False
    assert msg["payload"]["team_name"].startswith(f"expert-group-{GROUP_ID}-")
    record = metadata_store["s1"]
    assert record["expert_id"] == GROUP_ID
    assert record["expert_type"] == "team"
    assert record["mode"] == "team"
    assert record["team_name"] == msg["payload"]["team_name"]
    assert "team_template_id" in record


@pytest.mark.asyncio
async def test_load_team_idempotent(
        server, local_source: Path, metadata_store: dict, team_runtime
) -> None:
    """同团重复 load：幂等成功，不重写 metadata、不停运行时。"""
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": GROUP_ID,
        "expert_type": "team",
        "team_name": "expert-group-x-s1",
    }
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["applied"] is True
    assert metadata_store["__writes__"] == []
    assert team_runtime.stop_calls == []


@pytest.mark.asyncio
async def test_load_team_switch_from_single_expert(
        server, local_source: Path, metadata_store: dict, team_runtime,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    """单专家 → 团：先 apply_expert(None) 热卸载，再写团绑定。"""
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "expert-a",
        "expert_type": "agent",
    }
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    _install_fake_adapters(monkeypatch, server, _live_root(child))

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["previous_expert_id"] == "expert-a"
    child.apply_expert.assert_awaited_once()
    assert child.apply_expert.await_args.args[0] is None
    # 有活会话 → 停 team 运行时保证下次 chat 冷重建
    assert team_runtime.stop_calls != []
    assert metadata_store["s1"]["expert_type"] == "team"
    # 粘性留痕：绑团即写 was_expert_type="team"（卸载后保留，供切换弹窗跨重启判定）
    assert metadata_store["s1"]["was_expert_type"] == "team"
    # 最近绑定记录：last_expert_id 同步（卸载保留，归档成员面板 roster 解析源）
    assert metadata_store["s1"]["last_expert_id"] == GROUP_ID


@pytest.mark.asyncio
async def test_load_team_switch_from_other_team(
        server, local_source: Path, metadata_store: dict, team_runtime
) -> None:
    """团 A → 团 B：先停旧团运行时（先卸后装）。"""
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "other-group",
        "expert_type": "team",
        "team_name": "expert-group-other-group-s1",
    }
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["previous_expert_id"] == "other-group"
    assert any(c["reason"] == "expert.switch" for c in team_runtime.stop_calls)
    assert metadata_store["s1"]["expert_id"] == GROUP_ID


@pytest.mark.asyncio
async def test_load_team_busy_by_adapter(
        server, local_source: Path, metadata_store: dict, team_runtime,
        monkeypatch: pytest.MonkeyPatch,
) -> None:
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "", "expert_type": "agent"}
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    root = _live_root(child)
    root.expert_switch_blocked = lambda sid: True
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    assert ws.sent[0]["payload"]["code"] == "BUSY"
    assert metadata_store["__writes__"] == []


@pytest.mark.asyncio
async def test_load_team_busy_by_team_round(
        server, local_source: Path, metadata_store: dict, team_runtime
) -> None:
    """team 回合活跃信号命中：即使适配器侧不忙也 BUSY。"""
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "", "expert_type": "agent"}
    team_runtime.stream_active = True
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    assert ws.sent[0]["payload"]["code"] == "BUSY"
    assert metadata_store["__writes__"] == []


@pytest.mark.asyncio
async def test_load_team_session_not_found(
        server, local_source: Path, metadata_store: dict, team_runtime
) -> None:
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "ghost", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    assert ws.sent[0]["payload"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_unload_team_clears_binding(
        server, metadata_store: dict, team_runtime
) -> None:
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": GROUP_ID,
        "expert_type": "team",
        "was_expert_type": "team",
        "last_expert_id": GROUP_ID,
        "team_name": "expert-group-x-s1",
        "team_template_id": "expert_group",
        "mode": "team",
    }
    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["type"] == "team"
    assert msg["payload"]["previous_expert_id"] == GROUP_ID
    assert any(c["reason"] == "expert.unload" for c in team_runtime.stop_calls)
    record = metadata_store["s1"]
    assert record["expert_id"] == ""
    assert record["expert_type"] == "agent"
    assert record["team_name"] == ""
    assert record["team_template_id"] == ""
    assert record["mode"] == "agent"
    # 卸载四字段全清，但 was_expert_type 留痕保留（"该会话用过团协作"）
    assert record["was_expert_type"] == "team"
    # last_expert_id 同样保留（归档成员面板的 roster 解析源，跨重启不丢）
    assert record["last_expert_id"] == GROUP_ID


@pytest.mark.asyncio
async def test_unload_team_busy(
        server, metadata_store: dict, team_runtime
) -> None:
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": GROUP_ID,
        "expert_type": "team",
    }
    team_runtime.stream_active = True
    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    assert ws.sent[0]["payload"]["code"] == "BUSY"
    assert metadata_store["s1"]["expert_id"] == GROUP_ID


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_mode", "expected_mode"),
    [
        ("work", "team"),
        ("code", "code.team"),
        ("design", "design.team"),
    ],
)
async def test_load_team_canonical_follows_work_mode(
        server, local_source: Path, metadata_store: dict, team_runtime,
        work_mode: str, expected_mode: str,
) -> None:
    """装团按会话 work_mode 查 WorkModeProfile 注册表收敛 canonical。"""
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "",
        "expert_type": "agent",
        "work_mode": work_mode,
    }
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": GROUP_ID}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert metadata_store["s1"]["mode"] == expected_mode


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("work_mode", "team_mode", "expected_restore"),
    [
        ("work", "team", "agent"),
        ("code", "code.team", "code.normal"),
        ("design", "design.team", "design"),
    ],
)
async def test_unload_team_restores_single_canonical_by_work_mode(
        server, metadata_store: dict, team_runtime,
        work_mode: str, team_mode: str, expected_restore: str,
) -> None:
    """退团按会话 work_mode 恢复单 agent canonical（修 code/design 降级缺陷）。"""
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": GROUP_ID,
        "expert_type": "team",
        "team_name": "expert-group-x-s1",
        "team_template_id": "expert_group",
        "mode": team_mode,
        "work_mode": work_mode,
    }
    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    assert ws.sent[0]["ok"] is True
    assert metadata_store["s1"]["mode"] == expected_restore


@pytest.mark.asyncio
async def test_replay_skips_team_expert(
        metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """expert_type=team 时 _replay_expert_from_metadata 必须直接返回（防污染）。"""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": GROUP_ID,
        "expert_type": "team",
    }
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = "s1"
    apply_mock = AsyncMock()
    monkeypatch.setattr(adapter, "_apply_expert", apply_mock)

    await adapter._replay_expert_from_metadata()

    apply_mock.assert_not_awaited()


@pytest.mark.asyncio
async def test_replay_single_expert_regression(
        metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """单专家重放不受影响（回归）。"""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "expert-a",
        "expert_type": "agent",
    }
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._parent_session_id = "s1"
    apply_mock = AsyncMock()
    monkeypatch.setattr(adapter, "_apply_expert", apply_mock)

    await adapter._replay_expert_from_metadata()

    apply_mock.assert_awaited_once()


@pytest.mark.asyncio
async def test_chat_non_team_mode_rejected_when_team_bound(
        metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """D2：已绑团会话收到非 team 系 mode → chat.error BAD_REQUEST，不静默改道。"""
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        JiuWenSwarmDeepAdapter,
    )

    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": GROUP_ID,
        "expert_type": "team",
    }
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._instance = object()
    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda model: True)

    request = AgentRequest(
        request_id="r1",
        channel_id="desktop",
        session_id="s1",
        params={"query": "hi", "mode": "agent"},
        metadata={},
    )

    chunks = [
        chunk
        async for chunk in adapter.process_message_stream_impl(request, {})
    ]

    assert len(chunks) == 1
    assert chunks[0].payload["event_type"] == "chat.error"
    assert chunks[0].payload["code"] == "BAD_REQUEST"
    assert chunks[0].is_complete is True


def test_build_team_name_strips_channel_prefix() -> None:
    """桌面会话 id 形如 desktop_<ts>_<uuid>，[:8] 会切到恒定前缀 desktop_ ——
    必须剥渠道段，否则同渠道所有会话撞名共用 team DB（建表竞态同根）。"""
    from jiuwenswarm.server.runtime.expert.expert_service import (
        build_expert_group_team_name,
    )

    name_a = build_expert_group_team_name("stock-partner-team", "desktop_19abc001_deadbeef01")
    name_b = build_expert_group_team_name("stock-partner-team", "desktop_19abc002_cafef00d02")
    assert name_a != name_b
    assert name_a.startswith("expert-group-stock-partner-team-")
    assert "desktop" not in name_a  # 渠道前缀不应进入 team_name


def test_build_team_name_rejects_empty_session_id() -> None:
    from jiuwenswarm.server.runtime.expert.expert_service import (
        build_expert_group_team_name,
    )

    with pytest.raises(ValueError, match="session_id"):
        build_expert_group_team_name("grp", "")


def test_build_team_name_no_underscore_id() -> None:
    from jiuwenswarm.server.runtime.expert.expert_service import (
        build_expert_group_team_name,
    )

    assert build_expert_group_team_name("grp", "plain-session-id-123") == (
        "expert-group-grp-plain-se"
    )


@pytest.mark.parametrize(
    ("metadata_mode", "expected_manager_mode"),
    [
        ("agent", "agent"),
        ("code.normal", "code"),
        ("code.plan", "code"),
        ("design", "design"),
        ("team", "team"),
        ("code.team", "code"),
        ("design.team", "design"),
        ("team.plan", "code"),
        # 未知值原样透传（保持旧行为）
        ("mystery.mode", "mystery.mode"),
    ],
)
def test_locate_session_adapter_converts_canonical_to_manager_mode(
        metadata_mode: str, expected_manager_mode: str
) -> None:
    """回归：metadata 存 canonical（code.normal），AgentManager 按 manager mode 注册。

    修复前 code 会话拿 canonical 直查命中失败 → expert.unload 退化为
    「只清 metadata 不碰活实例」，活实例提示词卸不掉。
    """
    from jiuwenswarm.server.runtime.expert.expert_service import ExpertService

    captured: dict = {}

    class _AM:
        def get_agent_nowait(
                self, channel_id="", mode=None, project_dir=None, sub_mode=None
        ):
            captured["mode"] = mode
            return None

    svc = ExpertService(agent_manager=_AM(), adapter_resolver=lambda agent: None)
    svc._locate_session_adapter("desktop", "s1", mode=metadata_mode)
    assert captured["mode"] == expected_manager_mode
