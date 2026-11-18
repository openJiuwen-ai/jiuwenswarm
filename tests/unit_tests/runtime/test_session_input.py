# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""New Session inputs must neither wait for nor resume the current task."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse, AgentResponseChunk
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.context import get_current_runtime
from jiuwenswarm.runtime.service import AgentRuntime, RuntimeStateError
from jiuwenswarm.runtime.session.model import SessionExecutionState, SessionWorkKind


@pytest.fixture(autouse=True)
def isolated_agent_dirs(tmp_path, monkeypatch):
    """会话/lifecycle 持久状态落在 tmp，而不是真实用户主目录。

    claim_runtime 按 get_agent_sessions_dir 的父目录写 runtime owner 文件；
    不隔离时并行 worker 会通过共享文件争抢 "input-session" 的 owner
    （assert_runtime_owner 报 another live AgentServer owns）。
    """
    sessions = tmp_path / "agent" / "sessions"
    sessions.mkdir(parents=True)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_sessions_dir", lambda: sessions
    )


def request(rid="supplement", *, input_mode="steer", **params):
    return AgentRequest(
        request_id=rid, channel_id="web", session_id="input-session",
        req_method=ReqMethod.CHAT_SEND, is_stream=True,
        params={"query": rid, "mode": "agent", **({"input_mode": input_mode} if input_mode else {}), **params},
    )


async def collect(stream):
    return [event async for event in stream]


class GatedAgent:
    def __init__(self):
        self.entered = asyncio.Event()
        self.release = asyncio.Event()
        self.delivered = []
        self.cancelled = False
        self.fail_input = False
        self.hold_input = False
        self.input_entered = asyncio.Event()
        self.input_cancelled = asyncio.Event()
        self.control_entered = asyncio.Event()
        self.control_params: list[dict] = []
        self.question = False

    async def execute_message(self, req):
        async for chunk in self.process_message_stream(req):
            if chunk.is_complete:
                return AgentResponse(request_id=req.request_id, channel_id=req.channel_id, payload=chunk.payload)

    async def process_message_stream(self, req):
        self.entered.set()
        if self.question:
            yield AgentResponseChunk(
                request_id=req.request_id, channel_id=req.channel_id,
                payload={"event_type": "chat.ask_user_question", "request_id": "question-1"},
            )
            return
        try:
            await self.release.wait()
            yield AgentResponseChunk(
                request_id=req.request_id, channel_id=req.channel_id,
                payload={"event_type": "chat.final", "content": "original completed"}, is_complete=True,
            )
        except asyncio.CancelledError:
            self.cancelled = True
            raise

    async def deliver_session_input(self, req):
        assert get_current_runtime() is not None
        self.input_entered.set()
        if self.fail_input:
            raise ValueError("input rejected")
        if self.hold_input:
            try:
                await asyncio.Event().wait()
            finally:
                self.input_cancelled.set()
        self.delivered.append(req)
        yield AgentResponseChunk(
            request_id=req.request_id, channel_id=req.channel_id,
            payload={"event_type": "runtime.accepted", "request_id": req.request_id},
        )

    async def deliver_control_input(self, req):
        """控制通道投递（文本→自定义答案的 CONTROL_INPUT 流级路径）。

        atomgit 2026-11-18 #201711504：夹具此前缺本方法，控制投递在
        _stream_control_started 处抛 RuntimeError 被上游吞掉，流级用例恒绿。
        """
        assert get_current_runtime() is not None
        self.control_entered.set()
        self.control_params.append(dict(req.params or {}))
        yield AgentResponseChunk(
            request_id=req.request_id, channel_id=req.channel_id,
            payload={"event_type": "runtime.accepted", "request_id": req.request_id},
        )


@pytest.fixture
async def setup_runtime(monkeypatch):
    agent = GatedAgent()
    manager = SimpleNamespace(
        cancel_all_inflight_work=AsyncMock(), cleanup=AsyncMock(),
        begin_foreground_chat=AsyncMock(), end_foreground_chat=AsyncMock(),
        get_agent_for_session_nowait=Mock(return_value=agent),
        # _borrow_paused_agent 走 borrow_agent_nowait（atomgit #201711504）
        borrow_agent_nowait=Mock(side_effect=lambda borrowed: borrowed),
    )
    plan = SimpleNamespace(
        active_sessions=set(),
        ensure_state=AsyncMock(return_value=SimpleNamespace(events=[])),
        check_post_process_exit=AsyncMock(return_value=[]), reset_session=Mock(),
    )
    runtime = AgentRuntime(agent_manager=manager, initializer=AsyncMock(), plan_controller=plan)
    monkeypatch.setattr(runtime, "_prepare_chat_turn", AsyncMock(return_value=("agent", None, agent)))
    monkeypatch.setattr(runtime, "_trigger_before_chat_request_hook", AsyncMock())
    await runtime._register_session(session_id="input-session", channel_id="web")
    yield runtime, agent, manager
    agent.release.set()
    await runtime.close()


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
@pytest.mark.parametrize("mode", ["steer", "follow_up", "alias"])
async def test_new_input_bypasses_busy_lane_without_answer_or_second_agent(setup_runtime, unary, mode):
    runtime, agent, manager = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    supplement = (request(input_mode=None, runtime_mode=" STEER ")
                  if mode == "alias" else request(input_mode=mode))
    # The originating channel differs from the cached owner's channel.
    supplement.channel_id = "internal"
    supplement.is_stream = not unary
    events = await asyncio.wait_for(
        runtime.invoke(supplement) if unary else collect(runtime.stream(supplement)), 2,
    )
    assert [e.event_type for e in events] == ["runtime.accepted"]
    assert events[0].request_id == "supplement"
    assert events[0].channel_id == "internal"
    assert not original.done() and not agent.cancelled
    assert [r.request_id for r in agent.delivered] == ["supplement"]
    manager.get_agent_for_session_nowait.assert_called_once_with("web", "input-session")
    runtime._prepare_chat_turn.assert_awaited_once()
    snapshot = runtime._session_coordinator.snapshot_session("input-session")
    root = next(e for e in snapshot.executions if e.request_id == "original")
    child = next(e for e in snapshot.executions if e.request_id == "supplement")
    assert root.state is SessionExecutionState.RUNNING
    assert child.parent_execution_id == root.execution_id
    assert child.work_kind is SessionWorkKind.SESSION_INPUT
    agent.release.set()
    assert (await asyncio.wait_for(original, 2))[-1].payload["content"] == "original completed"


@pytest.mark.asyncio
@pytest.mark.parametrize("protected_mode", ["plan", "goal", "agent.code.plan", "agent.work.plan"])
@pytest.mark.parametrize("busy", [False, True])
async def test_protected_cross_session_input_never_uses_active_or_idle_delivery(
    setup_runtime, protected_mode, busy,
):
    from agent_gateway.common.session_message import SESSION_MESSAGE_INTERNAL_KEY
    from jiuwenswarm.runtime.session_input import SessionInputQueueRequiredError

    runtime, agent, manager = setup_runtime
    original = None
    if busy:
        original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
        await asyncio.wait_for(agent.entered.wait(), 2)
    if protected_mode == "plan":
        runtime.plan_controller.active_sessions.add("input-session")
    elif protected_mode == "goal":
        manager.has_active_goal = Mock(return_value=True)
    supplement = request(**{SESSION_MESSAGE_INTERNAL_KEY: {"message_id": "sm-steer"}})
    if protected_mode.startswith("agent."):
        supplement.params["mode"] = protected_mode
    with pytest.raises(SessionInputQueueRequiredError):
        await collect(runtime.stream(supplement))
    assert not agent.delivered
    assert not agent.cancelled
    if original is not None:
        assert not original.done()
        agent.release.set()
        await asyncio.wait_for(original, 2)
    else:
        assert not agent.entered.is_set()


@pytest.mark.asyncio
async def test_input_rejection_does_not_end_original(setup_runtime):
    runtime, agent, _ = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    agent.fail_input = True
    with pytest.raises(ValueError, match="input rejected"):
        await collect(runtime.stream(request()))
    assert not original.done() and not agent.cancelled
    agent.release.set()
    await original


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
async def test_idle_input_owns_normal_execution_and_output(setup_runtime, unary):
    runtime, agent, manager = setup_runtime
    agent.release.set()
    req = request()
    req.is_stream = not unary
    events = await runtime.invoke(req) if unary else await collect(runtime.stream(req))
    if not unary:
        assert events[0].event_type == "runtime.accepted"
        assert events[0].payload["input_delivery"] == "chat"
        assert events[0].request_id == req.request_id
        assert events[0].payload["execution_id"] == events[-1].payload["execution_id"]
    assert events[-1].event_type == "chat.final"
    manager.get_agent_for_session_nowait.assert_not_called()
    runtime._prepare_chat_turn.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
@pytest.mark.parametrize("query", ["", "Compare this screenshot"])
async def test_idle_image_steer_is_rejected_before_ack_or_execution(setup_runtime, tmp_path, monkeypatch, unary, query):
    from jiuwenswarm.runtime.session_input import SessionInputTargetError
    from jiuwenswarm.server.runtime.attachments import steer_images

    runtime, agent, _ = setup_runtime
    other_upload = tmp_path / "agent" / "sessions" / "other-session" / "uploads" / "private.png"
    other_upload.parent.mkdir(parents=True)
    other_upload.write_bytes(b"private image bytes must not be read")
    resolve_images = Mock(side_effect=AssertionError("idle image references must not be read"))
    monkeypatch.setattr(steer_images, "resolve_steer_image_references", resolve_images)
    req = request(query=query, media_items=[{
        "type": "image", "path": str(other_upload), "filename": other_upload.name,
        "mime_type": "image/png",
    }])
    req.is_stream = not unary
    received = []
    with pytest.raises(SessionInputTargetError, match="requires an active execution") as rejected:
        if unary:
            received.extend(await runtime.invoke(req))
        else:
            async for event in runtime.stream(req):
                received.append(event)
    assert rejected.value.code == "SESSION_INPUT_TARGET_CHANGED"
    assert received == []
    runtime._prepare_chat_turn.assert_not_awaited()
    resolve_images.assert_not_called()
    assert not agent.entered.is_set() and not agent.delivered
    assert other_upload.read_bytes() == b"private image bytes must not be read"


@pytest.mark.asyncio
async def test_image_only_steer_still_routes_to_current_execution(setup_runtime):
    runtime, agent, _ = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    images = [{"type": "image", "path": "/session/uploads/shot.png", "filename": "shot.png", "mime_type": "image/png"}]
    events = await collect(runtime.stream(request("image-supplement", query="", media_items=images)))
    assert [event.event_type for event in events] == ["runtime.accepted"]
    assert agent.delivered[-1].params["media_items"] == images
    assert not original.done() and not agent.cancelled
    agent.release.set()
    await original


@pytest.mark.asyncio
async def test_input_does_not_answer_or_supersede_pending_interaction(setup_runtime):
    runtime, agent, _ = setup_runtime
    agent.question = True
    await collect(runtime.stream(request("original", input_mode=None)))
    with pytest.raises(RuntimeError, match="waiting for an interaction answer"):
        await collect(runtime.stream(request()))
    root = runtime._session_coordinator.snapshot_session("input-session").executions[0]
    assert root.state is SessionExecutionState.WAITING_FOR_CONTROL
    assert root.waiting_control_ids == {"question-1"}
    assert not agent.delivered


@pytest.mark.asyncio
async def test_close_cancels_inflight_input_and_cannot_reopen_session(setup_runtime):
    runtime, agent, _ = setup_runtime
    agent.hold_input = True
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    supplement = asyncio.create_task(collect(runtime.stream(request())))
    await asyncio.wait_for(agent.input_entered.wait(), 2)
    await runtime._session_coordinator.close_session("input-session")
    results = await asyncio.gather(original, supplement, return_exceptions=True)
    assert all(isinstance(result, asyncio.CancelledError) for result in results)
    assert agent.input_cancelled.is_set()
    with pytest.raises(RuntimeStateError, match="closed"):
        await collect(runtime.stream(request("late")))


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [{"query": " "}, {"images": ["image.png"]}, {"mode": "team"}])
async def test_unsupported_inputs_fail_without_dispatch(setup_runtime, params):
    runtime, agent, _ = setup_runtime
    with pytest.raises(ValueError):
        await collect(runtime.stream(request(**params)))
    assert not agent.delivered
    runtime._prepare_chat_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_unowned_input_does_not_create_another_agent(setup_runtime):
    runtime, agent, _ = setup_runtime
    supplement = request()
    supplement.session_id = "other-runtime-session"
    with pytest.raises(RuntimeStateError, match="not owned"):
        await collect(runtime.stream(supplement))
    runtime._prepare_chat_turn.assert_not_awaited()
    assert not agent.delivered


@pytest.mark.asyncio
async def test_conflicting_modes_fail_without_dispatch(setup_runtime):
    runtime, _, _ = setup_runtime
    with pytest.raises(ValueError, match="must agree"):
        await collect(runtime.stream(request(runtime_mode="follow_up")))
    runtime._prepare_chat_turn.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("unary", [False, True])
async def test_bound_input_keeps_original_execution_and_returns_its_id(setup_runtime, unary):
    runtime, agent, _ = setup_runtime
    original = asyncio.create_task(collect(runtime.stream(request("original", input_mode=None))))
    await asyncio.wait_for(agent.entered.wait(), 2)
    before = runtime._session_coordinator.snapshot_session("input-session")
    target = before.executions[0].execution_id
    req = request(expected_execution_id=target)
    req.is_stream = not unary
    events = await runtime.invoke(req) if unary else await collect(runtime.stream(req))
    assert [event.event_type for event in events] == ["runtime.accepted"]
    assert events[0].payload["execution_id"] == target
    after = runtime._session_coordinator.snapshot_session("input-session")
    assert len([item for item in after.executions if item.work_kind is SessionWorkKind.CHAT_STREAM]) == 1
    assert not original.done() and not agent.cancelled
    agent.release.set()
    assert (await original)[-1].payload["execution_id"] == target


@pytest.mark.asyncio
@pytest.mark.parametrize("state", ["idle", "ended", "different_execution"])
async def test_bound_input_never_falls_back_or_targets_replacement(setup_runtime, state):
    runtime, agent, _ = setup_runtime
    target = "missing-execution"
    if state == "ended":
        agent.release.set()
        events = await collect(runtime.stream(request("original", input_mode=None)))
        target = events[-1].payload["execution_id"]
    original = None
    if state == "different_execution":
        original = asyncio.create_task(collect(runtime.stream(request("replacement", input_mode=None))))
        await asyncio.wait_for(agent.entered.wait(), 2)
    before = runtime._session_coordinator.snapshot_session("input-session")
    with pytest.raises(RuntimeError, match="targeted execution has ended or changed"):
        await collect(runtime.stream(request(expected_execution_id=target)))
    after = runtime._session_coordinator.snapshot_session("input-session")
    assert after.executions == before.executions
    assert not agent.delivered and not agent.cancelled
    if original:
        assert not original.done()
        agent.release.set()
        await original


@pytest.mark.asyncio
@pytest.mark.parametrize("params", [
    {"expected_execution_id": ""}, {"expected_execution_id": 42},
    {"expected_execution_id": "original", "input_mode": "follow_up"},
])
async def test_invalid_execution_binding_is_rejected(setup_runtime, params):
    runtime, agent, _ = setup_runtime
    with pytest.raises(ValueError, match="expected_execution_id"):
        await collect(runtime.stream(request(**params)))
    assert not agent.delivered
    runtime._prepare_chat_turn.assert_not_awaited()


@pytest.mark.asyncio
async def test_free_text_converts_only_for_single_other_question(setup_runtime):
    """#6792: 唯一文本可答题（带 Other 或无选项）才转自定义答案；歧义/固定项保持原判。"""
    runtime, agent, _ = setup_runtime
    agent.question = True
    await collect(runtime.stream(request("original", input_mode=None)))
    snapshot = runtime._session_coordinator.snapshot_session("input-session").executions[0]
    assert snapshot.state is SessionExecutionState.WAITING_FOR_CONTROL

    runtime.pending_interactions = AsyncMock(return_value=[{
        "request_id": "question-1",
        "interrupt_id": "int-1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "选个颜色",
                       "options": [{"label": "Other", "description": "Custom input"}]}],
    }])
    converted = await runtime._free_text_as_custom_answer(request("text-reply"))
    assert converted is not None
    assert converted.params["source"] == "ask_user_interrupt"
    assert converted.params["request_id"] == "question-1"
    assert converted.params["interrupt_id"] == "int-1"
    assert converted.params["answers"] == [
        {"question": "选个颜色", "selected_options": [], "custom_input": "text-reply"}
    ]

    runtime.pending_interactions = AsyncMock(return_value=[{
        "request_id": "question-1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "固定项", "options": [{"label": "A", "value": "a"}]}],
    }])
    assert await runtime._free_text_as_custom_answer(request("text-reply")) is None

    # 无按钮选项的开放提问：文本即唯一答案（#10451 检视 P2）
    runtime.pending_interactions = AsyncMock(return_value=[{
        "request_id": "question-1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "给出目标路径", "options": []}],
    }])
    converted = await runtime._free_text_as_custom_answer(request("/data/target"))
    assert converted is not None
    assert converted.params["answers"] == [
        {"question": "给出目标路径", "selected_options": [], "custom_input": "/data/target"}
    ]

    # 多个可答题并存：不猜第一个，保持全量等待（#10451 检视 P2）
    runtime.pending_interactions = AsyncMock(return_value=[
        {"request_id": "q-1", "source": "ask_user_interrupt",
         "questions": [{"question": "A", "options": [{"label": "Other"}]}]},
        {"request_id": "q-2", "source": "ask_user_interrupt",
         "questions": [{"question": "B", "options": [{"label": "Other"}]}]},
    ])
    assert await runtime._free_text_as_custom_answer(request("text-reply")) is None

    runtime.pending_interactions = AsyncMock(return_value=[{
        "request_id": "question-1",
        "source": "ask_user_interrupt",
        "questions": [
            {"question": "一", "options": [{"label": "Other"}]},
            {"question": "二", "options": [{"label": "Other"}]},
        ],
    }])
    assert await runtime._free_text_as_custom_answer(request("text-reply")) is None

    runtime.pending_interactions = AsyncMock(return_value=[])
    assert await runtime._free_text_as_custom_answer(request("text-reply")) is None

    runtime.pending_interactions = AsyncMock(return_value=[{
        "request_id": "question-1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "选个颜色", "options": [{"label": "Other"}]}],
    }])
    assert await runtime._free_text_as_custom_answer(
        request("text-reply", expected_execution_id=snapshot.execution_id)) is None
    assert await runtime._free_text_as_custom_answer(
        request("text-reply", query="", content="随手补充")) is not None


@pytest.mark.asyncio
async def test_permission_questions_never_consume_free_text(setup_runtime):
    """P2 回归（atomgit 2026-11-18）：source 非 ask_user_interrupt 的提问不得被
    自由文本绑定。权限/确认提问在 options 缺失或非 dict 形态下按纯形态判定
    也会被误判「文本可答」，随手输入的文本即构成危险操作代答。
    修复前：converted is not None＝红。"""
    runtime, agent, _ = setup_runtime
    agent.question = True
    await collect(runtime.stream(request("original", input_mode=None)))
    for bad_source in ("permission_interrupt", "confirm_interrupt", ""):
        runtime.pending_interactions = AsyncMock(return_value=[{
            "request_id": "perm-1",
            "source": bad_source,
            "questions": [{"question": "确认执行危险命令？", "options": []}],
        }])
        assert await runtime._free_text_as_custom_answer(request("确认")) is None, bad_source


@pytest.mark.asyncio
async def test_stream_routes_converted_text_into_control_channel(setup_runtime):
    """#10451 检视 P3：流级覆盖 SESSION_INPUT → CONTROL_INPUT 路由改写。"""
    runtime, agent, _ = setup_runtime
    agent.question = True
    await collect(runtime.stream(request("original", input_mode=None)))
    assert runtime._session_coordinator.unanswered_interactions("input-session")
    runtime.pending_interactions = AsyncMock(return_value=[{
        "request_id": "question-1",
        "source": "ask_user_interrupt",
        "questions": [{"question": "选个颜色", "options": [{"label": "Other"}]}],
    }])
    # input_mode 必须取会话输入语义（生产补充输入同形）；None 会分类为
    # CHAT_STREAM 新回合，转换路径根本不进入——原用例 input_mode=None 即
    # atomgit #201711504 点名的假绿根因（仅查错误串不出现恒真）。
    events = await collect(runtime.stream(request("text-reply")))
    rendered = json.dumps(events, ensure_ascii=False, default=str)
    assert "supplemental input was not sent" not in rendered
    # 强化断言（atomgit 2026-11-18 #201711504）：控制通道必须真实收到转换后的
    # 答案载荷，而非仅检查旧错误串不出现（夹具缺 deliver_control_input 时
    # 投递被吞、断言恒绿的假绿形态）。
    assert agent.control_entered.is_set(), "控制通道投递未发生（此前绿为假绿）"
    assert agent.control_params, "deliver_control_input 未收到请求"
    delivered = agent.control_params[-1]
    assert delivered["source"] == "ask_user_interrupt"
    assert delivered["request_id"] == "question-1"
    assert delivered["answers"] == [
        {"question": "选个颜色", "selected_options": [], "custom_input": "text-reply"}
    ]
    assert not agent.input_entered.is_set()  # 文本不得再进会话输入通道
