# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""服务端 plan_execute 幂等续跑测试（issue 5223）。

覆盖 S_04 §7 续跑锚点：识别真值表 / 幂等去重 / 状态隔离 / grace 取消 /
合成请求字段 / busy 避让 / 语言解析 / 编排挂点（_check_post_process_plan_exit）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.agent_ws_server import AgentWebSocketServer
from jiuwenswarm.server.runtime import plan_execute_continuation as pec

# grace/退避压到毫秒级：测试里真实跑计时任务但不拖慢用例。
_GRACE = 0.01


def _plan_execute_params(**extra) -> dict:
    params = {
        "query": "",
        "mode": "code.plan",
        "source": "confirm_interrupt",
        "request_id": "call_1",
        "answers": [{"selected_options": ["plan_execute"], "custom_input": ""}],
    }
    params.update(extra)
    return params


def _resume_request(session_id: str = "sess-plan-1", **extra) -> AgentRequest:
    return AgentRequest(
        request_id="req-1",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_SEND,
        params=_plan_execute_params(**extra),
    )


def _default_ctx(server, request):
    from jiuwenswarm.server.context import AgentServerServices, RequestContext
    from jiuwenswarm.server.transports.sink import WSSink

    class _NullWs:
        async def send(self, text):  # noqa: ANN001
            return None

    _ws = _NullWs()
    return RequestContext(
        request=request,
        sink=WSSink(_ws, asyncio.Lock()),
        connection_id=str(id(_ws)),
        services=AgentServerServices(server),
    )


def _fake_server():
    server = AgentWebSocketServer.__new__(AgentWebSocketServer)
    server.send_push = AsyncMock()
    return server


@pytest.fixture(autouse=True)
def _fast_and_clean(monkeypatch):
    """grace/退避压缩到毫秒级 + 每用例后清空记账，防跨用例泄漏。"""
    monkeypatch.setattr(pec, "PLAN_EXECUTE_CONTINUATION_GRACE_SECONDS", _GRACE)
    monkeypatch.setattr(pec, "_BUSY_RETRY_INTERVAL_SECONDS", 0.0)
    yield
    pec._pending.clear()
    pec._claimed.clear()
    pec._cancelled_keys.clear()


# ── 识别真值表 ───────────────────────────────────────────────────────────────

def test_plan_execute_resume_truth_table():
    assert pec.is_plan_execute_resume(_plan_execute_params()) is True
    # 选项列表里混入其他取值仍命中
    assert (
        pec.is_plan_execute_resume(
            _plan_execute_params(answers=[{"selected_options": ["plan_execute", "x"]}])
        )
        is True
    )
    # TUI approve / 跳过 / 修订都不命中
    for value in ("approve", "plan_skip", "reject"):
        assert (
            pec.is_plan_execute_resume(
                _plan_execute_params(answers=[{"selected_options": [value]}])
            )
            is False
        )
    # source 不对 / 缺 answers / 非 dict params
    assert pec.is_plan_execute_resume(_plan_execute_params(source="permission_interrupt")) is False
    assert (
        pec.is_plan_execute_resume(
            {k: v for k, v in _plan_execute_params().items() if k != "answers"}
        )
        is False
    )
    assert pec.is_plan_execute_resume(None) is False


# ── 幂等与状态隔离 ──────────────────────────────────────────────────────────

async def _run_pending_tasks():
    await asyncio.sleep(_GRACE * 5)
    await asyncio.sleep(0)

@pytest.mark.asyncio

async def test_schedule_is_idempotent_per_approval_round(monkeypatch):
    """同一审批轮重复触发（双事件/对账并发）→ 续跑恰好一次（issue 测试计划 1）。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    request = _resume_request()

    assert pec.schedule_plan_execute_continuation(None, request, object()) is True
    assert pec.schedule_plan_execute_continuation(None, request, object()) is False
    await _run_pending_tasks()

    assert run_stream.await_count == 1
    assert pec._pending == {}

@pytest.mark.asyncio

async def test_non_execute_answers_never_schedule(monkeypatch):
    """TUI approve / 跳过 / 修订不含 plan_execute 取值 → 不登记（状态隔离）。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    for value in ("approve", "plan_skip", "reject"):
        request = _resume_request(
            answers=[{"selected_options": [value], "custom_input": ""}]
        )
        assert pec.schedule_plan_execute_continuation(None, request, object()) is False
    await _run_pending_tasks()
    assert run_stream.await_count == 0
    assert pec._pending == {}

@pytest.mark.asyncio

async def test_normal_message_cancels_pending(monkeypatch):
    """grace 窗口内普通消息到达 → 取消续跑，由该消息充当执行消息。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    request = _resume_request()

    assert pec.schedule_plan_execute_continuation(None, request, object()) is True
    assert pec.cancel_plan_execute_continuation("sess-plan-1", reason="chat.send") == 1
    await _run_pending_tasks()

    assert run_stream.await_count == 0
    assert pec._pending == {}

@pytest.mark.asyncio

async def test_cancel_other_session_is_noop(monkeypatch):
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    request = _resume_request()
    pec.schedule_plan_execute_continuation(None, request, object())

    assert pec.cancel_plan_execute_continuation("another-session") == 0
    await _run_pending_tasks()
    assert run_stream.await_count == 1


@pytest.mark.asyncio
async def test_schedule_copies_trusted_identity_and_memory():
    """调度白名单抄送身份/记忆键与归档维度，一次性载荷不夹带（评审 CR-2）。"""
    metadata = {
        "user_id": "u-1",
        "routing": {"group_id": "g-1", "bot_id": "b-1", "gateway_id": "w-1"},
        "enable_memory": False,
        "one_shot_payload": "must-not-copy",
    }
    request = _resume_request()
    request.metadata = metadata
    request.thread_id = "thread_abc"
    request.chat_id = "chat_123"

    assert pec.schedule_plan_execute_continuation(None, request, object()) is True

    plan = pec._pending["sess-plan-1:call_1"]
    assert plan.trusted_metadata["user_id"] == "u-1"
    assert plan.trusted_metadata["enable_memory"] is False
    assert plan.trusted_metadata["routing"] == metadata["routing"]
    # routing 浅拷贝防别名：原请求后续改写不影响记账
    plan.trusted_metadata["routing"]["group_id"] = "mutated"
    assert metadata["routing"]["group_id"] == "g-1"
    assert "one_shot_payload" not in plan.trusted_metadata
    assert plan.thread_id == "thread_abc"
    assert plan.chat_id == "chat_123"


# ── 合成请求与推送 ──────────────────────────────────────────────────────────

class _FakeChunk:
    def __init__(self, payload, is_complete=False, request_id=""):
        self.payload = payload
        self.is_complete = is_complete
        self.request_id = request_id


class _FakeAgent:
    def __init__(self, chunks):
        self._chunks = chunks
        self.requests: list[AgentRequest] = []

    async def process_message_stream(self, request):
        self.requests.append(request)
        for chunk in self._chunks:
            yield chunk

@pytest.mark.asyncio

async def test_continuation_request_fields_and_pushes():
    """合成请求字段 + 回显帧/chunk 推送次序（issue 测试计划 2 的服务端侧）。"""
    chunk = _FakeChunk({"event_type": "chat.final", "content": "ok"}, is_complete=True)
    agent = _FakeAgent([chunk])
    ctx = SimpleNamespace(services=SimpleNamespace(send_push=AsyncMock()))
    plan = pec._ContinuationPlan(
        key="sess-plan-1:call_1",
        session_id="sess-plan-1",
        channel_id="web",
        round_id="call_1",
        plan_slug="slug-1",
        mode="code.normal",
        query="计划已批准，请按计划开始执行。",
        copied_params={"project_dir": "C:/work/proj", "work_mode": "code"},
        trusted_metadata={
            "user_id": "u-1",
            "routing": {"group_id": "g-1", "bot_id": "b-1"},
            "enable_memory": False,
        },
        thread_id="thread_abc",
        chat_id="chat_123",
    )

    await pec._run_continuation_stream(ctx, agent, plan)

    sent = agent.requests[0]
    assert sent.is_stream is True
    assert sent.session_id == "sess-plan-1"
    assert sent.channel_id == "web"
    assert sent.request_id.startswith("plan_execute_continuation_call_1_")
    assert sent.params["mode"] == "code.normal"
    assert sent.params["source"] == "plan_execute_continuation"
    assert sent.params["query"] == sent.params["display_query"] == plan.query
    # 上下文白名单抄送 + 不夹带一次性载荷
    assert sent.params["project_dir"] == "C:/work/proj"
    assert sent.params["work_mode"] == "code"
    assert "answers" not in sent.params
    assert "request_id" not in sent.params
    # 身份/记忆白名单抄送（评审 CR-2）：续跑与普通请求同源——
    # _bind_tenant_request_context 经 web_routing_identity 读同一 metadata 形状，
    # 配额策略选择随之一致；PC 记忆归档维度（thread_id）不丢。
    assert sent.thread_id == "thread_abc"
    assert sent.chat_id == "chat_123"
    assert sent.metadata is not plan.trusted_metadata
    assert sent.metadata["user_id"] == "u-1"
    assert sent.metadata["enable_memory"] is False
    assert sent.metadata["routing"] == {"group_id": "g-1", "bot_id": "b-1"}
    from jiuwenswarm.common.request_identity import web_routing_identity

    assert web_routing_identity(sent.metadata) == {
        "user_id": "u-1",
        "group_id": "g-1",
        "bot_id": "b-1",
    }

    pushes = ctx.services.send_push.await_args_list
    assert len(pushes) == 2
    echo = pushes[0].args[0]
    assert echo["payload"]["event_type"] == "chat.processing_status"
    assert echo["payload"]["is_processing"] is True
    assert echo["payload"]["content"] == plan.query
    assert echo["payload"]["source"] == "plan_execute_continuation"
    assert echo["session_id"] == "sess-plan-1"
    # chunk 帧回退到续跑 request_id，保证前端能对齐
    assert pushes[1].args[0]["request_id"] == sent.request_id
    assert pushes[1].args[0]["payload"] == chunk.payload
    assert pushes[1].args[0]["is_complete"] is True


# ── 显式中断取消与领取竞态（评审 CR-3 双保险）───────────────────────────────


def _bare_plan() -> pec._ContinuationPlan:
    return pec._ContinuationPlan(
        key="sess-plan-1:call_1",
        session_id="sess-plan-1",
        channel_id="web",
        round_id="call_1",
        plan_slug="slug-1",
        mode="code.normal",
        query="计划已批准，请按计划开始执行。",
        copied_params={},
    )


@pytest.mark.asyncio
async def test_cancel_while_claimed_tombstones_without_count():
    """已领取未代发条目被取消：记账移除 + 写墓碑，不计入待执行取消数（CR-3）。"""
    plan = _bare_plan()
    pec._pending[plan.key] = plan
    pec._claimed.add(plan.key)

    assert (
        pec.cancel_plan_execute_continuation(
            "sess-plan-1", reason="chat.interrupt:cancel"
        )
        == 0
    )
    assert plan.key not in pec._pending
    assert plan.key in pec._cancelled_keys


@pytest.mark.asyncio
async def test_pre_dispatch_tombstone_recheck_skips_dispatch(monkeypatch):
    """代发前墓碑复核：已领取且已取消 → 不代发，收尾清理三处状态（CR-3 双保险）。

    直接摆出「已领取 + 已取消墓碑」的记账形态跑真实 _grace_then_run：
    复核退出且 finally 清理记账/领取标记/墓碑，不累积。
    """
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    plan = _bare_plan()
    pec._pending[plan.key] = plan
    pec._claimed.add(plan.key)
    pec._cancelled_keys.add(plan.key)

    await pec._grace_then_run(SimpleNamespace(), object(), plan)

    assert run_stream.await_count == 0
    assert pec._pending == {}
    assert plan.key not in pec._claimed
    assert plan.key not in pec._cancelled_keys


@pytest.mark.asyncio
async def test_claimed_entry_dispatches_when_not_cancelled(monkeypatch):
    """已领取且未取消 → 正常代发；收尾清理领取标记与记账。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    plan = _bare_plan()
    pec._pending[plan.key] = plan
    pec._claimed.add(plan.key)

    await pec._grace_then_run(SimpleNamespace(), object(), plan)

    assert run_stream.await_count == 1
    assert pec._pending == {}
    assert not pec._claimed
    assert not pec._cancelled_keys


def _cancel_request(session_id="sess-plan-1", intent="cancel", metadata=None):
    return AgentRequest(
        request_id="interrupt-1",
        channel_id="web",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": intent},
        metadata=metadata,
        is_stream=False,
    )


def _cancel_ctx(request):
    """handle_chat_cancel_dispatch 只触及 request/agent_manager/sink/
    session_stream_tasks（test_chat_interrupt_team_cancel.py 同款 stub）。"""
    return SimpleNamespace(
        request=request,
        services=SimpleNamespace(
            agent_manager=SimpleNamespace(
                get_agent_nowait=MagicMock(return_value=None),
            ),
            session_stream_tasks={request.session_id or "default": {}},
        ),
        sink=AsyncMock(),
        connection_id="c1",
    )


@pytest.mark.asyncio
async def test_chat_interrupt_cancels_pending_continuation(monkeypatch):
    """显式中断（cancel/supplement）取消待执行续跑；断连形态不取消（CR-3）。"""
    from jiuwenswarm.common.e2a.constants import (
        E2A_CANCEL_SOURCE_CLIENT_DISCONNECT,
        E2A_INTERNAL_CANCEL_SOURCE_KEY,
    )
    from jiuwenswarm.server.handlers import chat as chat_handlers

    cancelled: list[tuple[str, str]] = []
    monkeypatch.setattr(
        chat_handlers,
        "cancel_plan_execute_continuation",
        lambda sid, *, reason="": cancelled.append((sid, reason)) or 0,
    )

    ctx = _cancel_ctx(_cancel_request(intent="cancel"))
    await chat_handlers.handle_chat_cancel_dispatch(ctx)
    assert cancelled == [("sess-plan-1", "chat.interrupt:cancel")]

    cancelled.clear()
    ctx = _cancel_ctx(_cancel_request(intent="supplement"))
    await chat_handlers.handle_chat_cancel_dispatch(ctx)
    assert cancelled == [("sess-plan-1", "chat.interrupt:supplement")]

    # pause/resume 任务仍在运行，不取消续跑
    for intent in ("pause", "resume"):
        cancelled.clear()
        ctx = _cancel_ctx(_cancel_request(intent=intent))
        await chat_handlers.handle_chat_cancel_dispatch(ctx)
        assert cancelled == []

    # 客户端断连保持继续执行语义（断连兜底是本特性的存在理由）
    cancelled.clear()
    ctx = _cancel_ctx(
        _cancel_request(
            metadata={
                E2A_INTERNAL_CANCEL_SOURCE_KEY: E2A_CANCEL_SOURCE_CLIENT_DISCONNECT
            }
        )
    )
    await chat_handlers.handle_chat_cancel_dispatch(ctx)
    assert cancelled == []


# ── busy 避让与语言 ─────────────────────────────────────────────────────────
@pytest.mark.asyncio

async def test_busy_session_gives_up_after_retries(monkeypatch):
    """会话持续忙 → 退避重试后放弃并清记账，不代发。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    busy_adapter = SimpleNamespace(
        is_deep_agent_executing_for_session=lambda sid: True  # noqa: ARG005
    )
    monkeypatch.setattr(
        AgentWebSocketServer, "resolve_adapter", staticmethod(lambda agent: busy_adapter)  # noqa: ARG005
    )
    request = _resume_request()

    assert pec.schedule_plan_execute_continuation(None, request, object()) is True
    # 3 次 × 0s 退避，留出事件循环调度余量
    await asyncio.sleep(0.05)

    assert run_stream.await_count == 0
    assert pec._pending == {}


@pytest.mark.asyncio
async def test_resolve_language_prefers_agent_builder(monkeypatch):
    """builder 挂在 adapter._instance 上（真实形状，评审 2026-11-09）：
    adapter 自身无 system_prompt_builder，直接读恒 None 恒回退 cn。"""
    en_agent = SimpleNamespace()
    monkeypatch.setattr(
        AgentWebSocketServer,
        "resolve_adapter",
        staticmethod(
            lambda agent: SimpleNamespace(
                _instance=SimpleNamespace(
                    system_prompt_builder=SimpleNamespace(language="en")
                )
            )
        ),  # noqa: ARG005
    )
    assert pec._resolve_language(en_agent) == "en"
    # _instance 上 builder 为 None / adapter 无 _instance → 回退 cn
    monkeypatch.setattr(
        AgentWebSocketServer,
        "resolve_adapter",
        staticmethod(
            lambda agent: SimpleNamespace(
                _instance=SimpleNamespace(system_prompt_builder=None)
            )
        ),  # noqa: ARG005
    )
    assert pec._resolve_language(en_agent) == "cn"
    monkeypatch.setattr(
        AgentWebSocketServer,
        "resolve_adapter",
        staticmethod(lambda agent: SimpleNamespace()),  # noqa: ARG005
    )
    assert pec._resolve_language(en_agent) == "cn"


@pytest.mark.asyncio
async def test_continuation_failure_pushes_terminal_frames(monkeypatch):
    """执行流抛异常 → 补推 chat.error + processing_status=false 终态帧。

    续跑无本地 chat.send 超时兜底，缺终态帧前端停在 processing=true
    （评审 2026-11-09）。走完整 _grace_then_run 路径（grace/退避已被
    fixture 压到毫秒级）。
    """
    agent = _FakeAgent([])

    async def _boom(request):
        agent.requests.append(request)
        raise RuntimeError("quota exceeded")
        yield  # pragma: no cover

    agent.process_message_stream = _boom
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    ctx = SimpleNamespace(services=SimpleNamespace(send_push=AsyncMock()))
    plan = pec._ContinuationPlan(
        key="sess-plan-1:call_1",
        session_id="sess-plan-1",
        channel_id="web",
        round_id="call_1",
        plan_slug="slug-1",
        mode="code.normal",
        query="计划已批准，请按计划开始执行。",
        copied_params={},
    )
    pec._pending[plan.key] = plan

    await pec._grace_then_run(ctx, agent, plan)
    await _run_pending_tasks()

    pushes = ctx.services.send_push.await_args_list
    event_types = [call.args[0]["payload"].get("event_type") for call in pushes]
    assert "chat.error" in event_types
    assert "chat.processing_status" in event_types
    error_frame = pushes[event_types.index("chat.error")].args[0]
    assert error_frame["payload"]["error"] == "quota exceeded"
    assert error_frame["session_id"] == "sess-plan-1"
    assert error_frame["request_id"].startswith("plan_execute_continuation_call_1_")
    status_frame = pushes[event_types.index("chat.processing_status", 1)].args[0]
    assert status_frame["payload"]["is_processing"] is False
    # 两帧共用同一 request_id，前端可对齐
    assert status_frame["request_id"] == error_frame["request_id"]
    assert pec._pending == {}


# ── 编排挂点：_check_post_process_plan_exit ────────────────────────────────

def _plan_exit_agent(mode: str = "normal"):
    """命中 ``_open_plan_state_session`` 的 live 分支。

    ``get_live_session_instance`` 返回带 ``_interaction_session`` 的 live
    agent（``resolve_live_agent_session`` 按该属性 + ``get_session_id`` 匹配）；
    ``load_state`` 返回可注入模式的 state。
    """
    live_session = SimpleNamespace(get_session_id=lambda: "sess-plan-1")
    live_agent = SimpleNamespace(
        _interaction_session=live_session,
        load_state=lambda session: SimpleNamespace(  # noqa: ARG005
            plan_mode=SimpleNamespace(mode=mode, plan_slug="slug-1")
        ),
    )
    return SimpleNamespace(
        get_live_session_instance=lambda session_id: live_agent,  # noqa: ARG005
        ensure_instance=AsyncMock(),
    )


async def _check_plan_exit(request, *, mode="normal"):
    from jiuwenswarm.server.handlers import _default, _shared

    server = _fake_server()
    ctx = _default_ctx(server, request)
    agent = _plan_exit_agent(mode=mode)
    try:
        await _default._check_post_process_plan_exit(ctx, request, agent)
    finally:
        _shared._plan_exited_sessions.discard(request.session_id)
        _shared._plan_active_sessions.discard(request.session_id)
    return server

@pytest.mark.asyncio

async def test_check_post_process_schedules_for_plan_execute(monkeypatch):
    """批准轮检出 plan→normal 且载荷含 plan_execute → 登记续跑（编排挂点）。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    request = _resume_request()

    server = await _check_plan_exit(request)
    await _run_pending_tasks()

    assert run_stream.await_count == 1
    # plan.mode_exited 照常推送，续跑在其后
    pushed_events = [call.args[0]["payload"].get("event_type") for call in server.send_push.await_args_list]
    assert "plan.mode_exited" in pushed_events

@pytest.mark.asyncio

async def test_check_post_process_is_idempotent_and_isolated(monkeypatch):
    """重复 post-process 只登记一次；非 plan_execute 载荷与 plan 态不登记。"""
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)

    request = _resume_request()
    await _check_plan_exit(request)
    await _check_plan_exit(request)  # 同审批轮二次检出（幂等）

    tui_request = _resume_request(
        answers=[{"selected_options": ["approve"], "custom_input": ""}]
    )
    await _check_plan_exit(tui_request)  # TUI approve：模式同样退出，但不续跑
    still_plan = _resume_request()
    await _check_plan_exit(still_plan, mode="plan")  # mode 未退出：不进分支
    await _run_pending_tasks()

    assert run_stream.await_count == 1
