# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""experts.list / expert.load / expert.unload WS handler 测试。

协议冻结点：payload 形状与错误码在此锁定。
"""

import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

import jiuwenswarm.server.agent_ws_server as server_module
import jiuwenswarm.server.runtime.session.session_metadata as sm
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.expert import expert_store as es


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


def _make_package(root: Path, name: str, **overrides) -> Path:
    pkg = root / name
    (pkg / "agents").mkdir(parents=True)
    (pkg / "agents" / "00-identity.md").write_text("# 人设", encoding="utf-8")
    manifest: dict = {
        "packageType": "agent_template",
        "agentCard": {"id": name, "name": f"{name} 专家", "description": "描述"},
        "persona": {"dir": "agents"},
        "metadata": {"tags": ["test"]},
    }
    manifest.update(overrides)
    (pkg / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return pkg


@pytest.fixture
def local_source(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "experts"
    root.mkdir()
    source = es.LocalDirExpertPackageSource(experts_dir=root)
    monkeypatch.setattr(es, "get_expert_source", lambda: source)
    return root


@pytest.fixture
def metadata_store(monkeypatch: pytest.MonkeyPatch) -> dict:
    """内存版 session metadata（全字段写入——只记 expert_id 会让
    "expert_type 未被改写"类断言恒真，回归拦截失效）。"""
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


def _install_fake_adapters(
        monkeypatch: pytest.MonkeyPatch,
        server: server_module.AgentWebSocketServer,
        root,
) -> None:
    monkeypatch.setattr(
        server._agent_manager, "get_agent_nowait", lambda channel_id, **_: object()
    )
    monkeypatch.setattr(server, "_resolve_adapter", lambda agent: root)


# ─── experts.list ─────────────────────────────────────


@pytest.mark.asyncio
async def test_list_returns_experts(
        server, local_source: Path
) -> None:
    _make_package(local_source, "security-reviewer")
    ws = FakeWebSocket()
    await server._handle_experts_list(
        ws, _request(ReqMethod.EXPERTS_LIST, {}), asyncio.Lock()
    )
    (msg,) = ws.sent
    assert msg["ok"] is True
    (expert,) = msg["payload"]["experts"]
    assert expert["id"] == "security-reviewer"
    assert expert["source"] == "local"
    assert expert["available"] is True
    assert expert["type"] == "agent"


@pytest.mark.asyncio
async def test_list_repo_unavailable(
        server, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _DownSource:
        async def list(self):
            raise es.ExpertRepoUnavailable("专家仓库不可达: connection refused")

    monkeypatch.setattr(es, "get_expert_source", lambda: _DownSource())
    ws = FakeWebSocket()
    await server._handle_experts_list(
        ws, _request(ReqMethod.EXPERTS_LIST, {}), asyncio.Lock()
    )
    (msg,) = ws.sent
    assert msg["ok"] is False
    assert msg["payload"]["code"] == "REPO_UNAVAILABLE"


# ─── expert.load ──────────────────────────────────────


@pytest.mark.asyncio
async def test_load_bad_request(server) -> None:
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws, _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    assert ws.sent[0]["payload"]["code"] == "BAD_REQUEST"


@pytest.mark.asyncio
async def test_load_not_found(
        server, local_source: Path, metadata_store: dict
) -> None:
    metadata_store["s1"] = {"session_id": "s1", "expert_id": ""}
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "nope"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["payload"]["code"] == "NOT_FOUND"
    assert metadata_store["__writes__"] == [], "失败不应写 metadata"


@pytest.mark.asyncio
async def test_load_invalid_package(
        server, local_source: Path, metadata_store: dict
) -> None:
    _make_package(
        local_source, "bad", rails=[{"file": "rails/x.py", "class": "X"}]
    )
    metadata_store["s1"] = {"session_id": "s1", "expert_id": ""}
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "bad"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["payload"]["code"] == "INVALID_PACKAGE"
    assert metadata_store["__writes__"] == []


@pytest.mark.asyncio
async def test_load_pending_when_no_adapter(
        server, local_source: Path, metadata_store: dict
) -> None:
    """子适配器不存在：只写 metadata，applied=false pending=true。"""
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "old-expert"}
    # get_agent_nowait 默认（无 agent）→ root None → child None
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["applied"] is False
    assert msg["payload"]["pending"] is True
    assert msg["payload"]["previous_expert_id"] == "old-expert"
    assert metadata_store["s1"]["expert_id"] == "expert-a"


@pytest.mark.asyncio
async def test_load_applied_when_child_exists(
        server, local_source: Path, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {"session_id": "s1", "expert_id": ""}
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    root = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: False,
    )
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["payload"]["applied"] is True
    assert msg["payload"]["pending"] is False
    child.apply_expert.assert_awaited_once()
    assert child.apply_expert.await_args.args[0] == "expert-a"
    assert metadata_store["s1"]["expert_id"] == "expert-a"


@pytest.mark.asyncio
async def test_load_busy_rejected(
        server, local_source: Path, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {"session_id": "s1", "expert_id": ""}
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    root = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: True,
    )
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["payload"]["code"] == "BUSY"
    child.apply_expert.assert_not_awaited()
    assert metadata_store["__writes__"] == [], "BUSY 不应写 metadata"


@pytest.mark.asyncio
async def test_load_failed_keeps_old_metadata(
        server, local_source: Path, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "old-expert"}
    child = SimpleNamespace(
        has_live_instance=lambda: True,
        apply_expert=AsyncMock(side_effect=RuntimeError("boom")),
    )
    root = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: False,
    )
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["payload"]["code"] == "LOAD_FAILED"
    assert metadata_store["s1"]["expert_id"] == "old-expert", (
        "LOAD_FAILED 应保持旧 metadata 值"
    )


@pytest.mark.asyncio
async def test_load_session_not_found(
        server, local_source: Path, metadata_store: dict
) -> None:
    _make_package(local_source, "expert-a")
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "ghost", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    assert ws.sent[0]["payload"]["code"] == "NOT_FOUND"


@pytest.mark.asyncio
async def test_load_single_expert_rejected_when_team_bound(
        server, local_source: Path, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """团会话内换单专家 → TEAM_BOUND 拒绝，零状态变更（规格：装团后只能退团）。

    拒绝闸在 pending/applied 分岔之前，两种路径都够不到写入；此处带活实例验证
    apply_expert 也不被触及。
    """
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "some-group",
        "expert_type": "team",
        "team_name": "expert-group-some-group-s1",
        "mode": "team",
    }
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    root = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: False,
    )
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is False
    assert msg["payload"]["code"] == "TEAM_BOUND"
    assert metadata_store["__writes__"] == [], "拒绝不得写 metadata"
    assert metadata_store["s1"]["expert_id"] == "some-group"
    assert metadata_store["s1"]["expert_type"] == "team"
    child.apply_expert.assert_not_awaited()


@pytest.mark.asyncio
async def test_load_single_expert_rejected_on_team_name_residue(
        server, local_source: Path, metadata_store: dict
) -> None:
    """双因子兜底：expert_type 已是 agent 但 team_name 残留（半截清理脏会话）
    → 同样拒绝，防漏进单专家分支继续污染。"""
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "some-group",
        "expert_type": "agent",
        "team_name": "expert-group-some-group-s1",
    }
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is False
    assert msg["payload"]["code"] == "TEAM_BOUND"
    assert metadata_store["__writes__"] == []


@pytest.mark.asyncio
async def test_load_team_bound_rejected_before_fetch(
        server, local_source: Path, metadata_store: dict
) -> None:
    """拒绝闸在 fetch 之前：团会话请求不存在的专家 → TEAM_BOUND（而非 NOT_FOUND），
    被拒请求不解析/不下载包。"""
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "some-group",
        "expert_type": "team",
        "team_name": "expert-group-some-group-s1",
    }
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "no-such-expert"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is False
    assert msg["payload"]["code"] == "TEAM_BOUND"


@pytest.mark.asyncio
async def test_load_single_rejected_when_team_binds_during_fetch(
        server, local_source: Path, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """fetch 后兜底：入口闸读取时未绑定，fetch 期间另一请求绑团（并发交错）
    → fetch 后重读复核 TEAM_BOUND，不放行污染。"""
    _make_package(local_source, "expert-a")
    clean = {"session_id": "s1", "expert_id": "", "expert_type": "agent"}
    bound = {
        "session_id": "s1",
        "expert_id": "some-group",
        "expert_type": "team",
        "team_name": "expert-group-some-group-s1",
        "mode": "team",
    }
    reads = iter([clean, bound])
    monkeypatch.setattr(
        sm,
        "get_session_metadata",
        lambda session_id, cache_bust=False, **_: dict(next(reads, bound)),
    )
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is False
    assert msg["payload"]["code"] == "TEAM_BOUND"


@pytest.mark.asyncio
async def test_load_rejected_on_team_canonical_mode(
        server, local_source: Path, metadata_store: dict
) -> None:
    """mode 族因子：expert_type/team_name 均空但 mode 是 team canonical（code.team，
    chat 成功回写先于 team_name 落盘的半截形态）→ 视为团队资产会话，拒绝。"""
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "",
        "expert_type": "agent",
        "mode": "code.team",
    }
    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["ok"] is False
    assert msg["payload"]["code"] == "TEAM_BOUND"
    assert metadata_store["__writes__"] == []


# ─── expert.unload ────────────────────────────────────


@pytest.mark.asyncio
async def test_unload_noop_without_expert(
        server, metadata_store: dict
) -> None:
    metadata_store["s1"] = {"session_id": "s1", "expert_id": ""}
    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    msg = ws.sent[0]
    assert msg["ok"] is True
    assert msg["payload"]["applied"] is False
    assert metadata_store["__writes__"] == []


@pytest.mark.asyncio
async def test_unload_applied(
        server, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "expert-a"}
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    root = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: False,
    )
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    msg = ws.sent[0]
    assert msg["payload"]["applied"] is True
    assert msg["payload"]["previous_expert_id"] == "expert-a"
    child.apply_expert.assert_awaited_once()
    assert child.apply_expert.await_args.args[0] is None
    assert metadata_store["s1"]["expert_id"] == ""


@pytest.mark.asyncio
async def test_unload_clears_metadata_when_no_adapter(
        server, metadata_store: dict
) -> None:
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "expert-a"}
    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    msg = ws.sent[0]
    assert msg["payload"]["applied"] is False, "无运行实例时只清 metadata"
    assert metadata_store["s1"]["expert_id"] == ""


@pytest.mark.asyncio
async def test_load_locates_root_by_session_metadata(
        server, local_source: Path, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    """同 channel 多 root（不同 project_dir）时，必须按会话 metadata 的
    mode/project_dir 定位——否则装载到错的 root 上，实际会话不生效。"""
    _make_package(local_source, "expert-a")
    metadata_store["s1"] = {
        "session_id": "s1",
        "expert_id": "",
        "mode": "agent",
        "project_dir": "D:/work/proj-b",
    }
    child_b = SimpleNamespace(
        has_live_instance=lambda: True, apply_expert=AsyncMock()
    )
    root_a = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: None,
        expert_switch_blocked=lambda sid: False,
    )
    root_b = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child_b,
        expert_switch_blocked=lambda sid: False,
    )
    agent_a, agent_b = object(), object()

    def _nowait(channel_id, mode=None, project_dir=None, sub_mode=None):
        # 模拟 AgentManager：带 project_dir 时按 key 命中 B，不带时退回第一个（A）
        return agent_b if project_dir == "D:/work/proj-b" else agent_a

    monkeypatch.setattr(server._agent_manager, "get_agent_nowait", _nowait)
    monkeypatch.setattr(
        server,
        "_resolve_adapter",
        lambda agent: root_b if agent is agent_b else root_a,
    )

    ws = FakeWebSocket()
    await server._handle_expert_load(
        ws,
        _request(ReqMethod.EXPERT_LOAD, {"session_id": "s1", "expert_id": "expert-a"}),
        asyncio.Lock(),
    )
    msg = ws.sent[0]
    assert msg["payload"]["applied"] is True
    child_b.apply_expert.assert_awaited_once(), "必须应用到 project_dir 匹配的 root 的子适配器上"


@pytest.mark.asyncio
async def test_unload_busy_rejected(
        server, metadata_store: dict, monkeypatch: pytest.MonkeyPatch
) -> None:
    metadata_store["s1"] = {"session_id": "s1", "expert_id": "expert-a"}
    child = SimpleNamespace(has_live_instance=lambda: True, apply_expert=AsyncMock())
    root = SimpleNamespace(
        is_session_scoped=False,
        get_cached_child_adapter=lambda sid: child,
        expert_switch_blocked=lambda sid: True,
    )
    _install_fake_adapters(monkeypatch, server, root)

    ws = FakeWebSocket()
    await server._handle_expert_unload(
        ws, _request(ReqMethod.EXPERT_UNLOAD, {"session_id": "s1"}), asyncio.Lock()
    )
    assert ws.sent[0]["payload"]["code"] == "BUSY"
    assert metadata_store["s1"]["expert_id"] == "expert-a"
