# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""服务端 plan_execute 幂等续跑测试（issue 5223）。

覆盖 S_04 §7 续跑锚点：识别真值表 / 幂等去重 / 状态隔离 / grace 取消 /
合成请求字段 / busy 避让 / 语言解析 / 编排挂点（_check_post_process_plan_exit）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

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


async def test_cancel_other_session_is_noop(monkeypatch):
    run_stream = AsyncMock()
    monkeypatch.setattr(pec, "_wait_until_session_idle", AsyncMock(return_value=True))
    monkeypatch.setattr(pec, "_run_continuation_stream", run_stream)
    request = _resume_request()
    pec.schedule_plan_execute_continuation(None, request, object())

    assert pec.cancel_plan_execute_continuation("another-session") == 0
    await _run_pending_tasks()
    assert run_stream.await_count == 1


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


# ── busy 避让与语言 ─────────────────────────────────────────────────────────

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


async def test_resolve_language_prefers_agent_builder(monkeypatch):
    en_agent = SimpleNamespace()
    monkeypatch.setattr(
        AgentWebSocketServer,
        "resolve_adapter",
        staticmethod(
            lambda agent: SimpleNamespace(
                system_prompt_builder=SimpleNamespace(language="en")
            )
        ),  # noqa: ARG005
    )
    assert pec._resolve_language(en_agent) == "en"
    monkeypatch.setattr(
        AgentWebSocketServer,
        "resolve_adapter",
        staticmethod(lambda agent: SimpleNamespace()),  # noqa: ARG005
    )
    assert pec._resolve_language(en_agent) == "cn"


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
