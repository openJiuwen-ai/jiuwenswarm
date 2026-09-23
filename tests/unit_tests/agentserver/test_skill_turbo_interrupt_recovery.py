# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo 中断恢复链路测试。

覆盖 #3755 修复三件套的可观察契约：
1. （已移除，2026-09）prepare_interrupt_artifacts_for_request 注入摘要 + 挂一次性
   hint + 清产物——三层兜底被 executor 的 plan_code_hash 主路径（fresh 清盘 /
   resume 重放 hash 匹配继承）物理取代，相关测试随之删除。
2. executor._clear_stale_node_artifacts：fresh 执行无条件清盘（不因残留 resume_ctx 跳过）。
3. process_interrupt(cancel/supplement)：清 pending 的 skill_acceleration_exec HITL 状态
   （INTERRUPTION_KEY + __skill_turbo_resume_ctx__），保留 node_artifacts。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter
from jiuwenswarm.server.runtime.skill_turbo import executor as executor_module
from jiuwenswarm.server.runtime.skill_turbo import node_artifact_store
from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    SKILL_TURBO_RESUME_CTX_KEY,
)


def _make_adapter(**state: object) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = None
    # process_interrupt（merge 自 e9013de6f）读取该属性，__init__ 跳过时须显式给值
    adapter._task_planning_rail = None
    for name, value in state.items():
        setattr(adapter, name, value)
    return adapter


# ────────────────── executor._clear_stale_node_artifacts ──────────────────


@pytest.mark.asyncio
async def test_clear_stale_node_artifacts_is_unconditional() -> None:
    """fresh 执行器启动必须无条件清盘，即使残留未消费的 resume_ctx。

    复现 #3755：全新任务（如"做长城PPT"接"做西湖PPT"）不得因上轮残留
    resume_ctx 跳过清盘，否则旧产物污染新任务（commit 1fe36d2d8 引入的回归）。
    """
    executor = object.__new__(executor_module.SkillTurboExecutor)
    executor._env = SimpleNamespace(card=MagicMock())

    clear_session = MagicMock()
    clear_session.session_id = "sess-clear"
    clear_session.pre_run = AsyncMock()
    clear_session.post_run = AsyncMock()
    # 模拟 checkpointer 中残留 resume_ctx（守卫派生实现会读到它并跳过清盘）
    clear_session.get_state = MagicMock(
        return_value={"plan_code": "plan()", "pending_tool_call_id": "tc-1"}
    )

    clear_spy = AsyncMock()
    with (
        patch.object(
            executor_module, "create_agent_session", return_value=clear_session
        ),
        patch.object(executor_module, "set_skill_turbo_id", MagicMock()),
        patch.object(executor_module, "clear_node_artifacts", clear_spy),
    ):
        token = executor_module._session_var.set(
            SimpleNamespace(session_id="sess-clear")
        )
        try:
            await executor._clear_stale_node_artifacts()
        finally:
            executor_module._session_var.reset(token)

    clear_spy.assert_awaited_once_with(clear_session)
    clear_session.post_run.assert_awaited_once()


# ────────────────── process_interrupt(cancel/supplement) 清理 ──────────────────


def _skill_turbo_interruption_state():
    tool_call = SimpleNamespace(id="call-0", name="skill_acceleration_exec")
    ai_message = SimpleNamespace(tool_calls=[tool_call])
    return SimpleNamespace(
        ai_message=ai_message,
        interrupted_tools={"call-0": SimpleNamespace(tool_call=tool_call)},
    )


def _loop_session_fixture(interruption_state):
    loop_session = MagicMock()
    loop_session.get_session_id.return_value = "tui_sess_1"
    loop_session.get_state.return_value = interruption_state
    loop_session.commit = AsyncMock()
    context = MagicMock()
    context.get_messages.return_value = [
        SimpleNamespace(tool_calls=[]),
        interruption_state.ai_message,
    ]
    context_engine = MagicMock()
    context_engine.get_context.return_value = context
    context_engine.save_contexts = AsyncMock()
    return loop_session, context_engine, context


def _build_interrupt_request(intent: str, session_id: str = "tui_sess_1") -> AgentRequest:
    return AgentRequest(
        request_id=f"req-{intent}",
        channel_id="tui",
        session_id=session_id,
        req_method=ReqMethod.CHAT_CANCEL,
        params={"intent": intent, "mode": "agent"},
    )


@pytest.mark.asyncio
async def test_interaction_cancel_clears_pending_skill_turbo_hitl_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cancel 必须清掉 pending 的 skill_acceleration_exec HITL 状态。

    复现 #3755 问题 2：ask_user 暂停中点中断后，INTERRUPTION_KEY 残留导致
    下一条纯文本被 handle_resume 挂成问卷 resume 作答 → rail 无法解析 →
    re-interrupt 同 tcid 重发问卷（前端 dedup 吞卡）→ UI 永久卡死。
    """
    interruption_state = _skill_turbo_interruption_state()
    loop_session, context_engine, context = _loop_session_fixture(interruption_state)

    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=True)
    instance.goal_manager = None
    instance.card = MagicMock(id="card-hitl")

    # 顺序跟踪：清 HITL 状态必须晚于 cancel_round 终止 round（fresh 执行中
    # cancel 时先清会被 executor 退出前的 resume_ctx 落盘覆盖）
    call_order: list[str] = []
    instance.cancel_round = AsyncMock(
        side_effect=lambda **_kw: call_order.append("cancel_round")
    )
    def _track_update_state(state: dict) -> None:
        if INTERRUPTION_KEY in state:
            call_order.append("clear_hitl")
    loop_session.update_state = MagicMock(side_effect=_track_update_state)
    # 追踪 commit 与 save_contexts 的相对顺序：commit 必须在 save_contexts
    # 之后，否则弹出悬挂 tool_call 后的 context 写回仅进内存、不落盘
    loop_session.commit = AsyncMock(side_effect=lambda: call_order.append("commit"))
    context_engine.save_contexts = AsyncMock(
        side_effect=lambda *_a, **_k: call_order.append("save_contexts")
    )

    skill_turbo_session = MagicMock()
    skill_turbo_session.pre_run = AsyncMock()
    skill_turbo_session.post_run = AsyncMock()
    # 真实空 session 的 get_state 返回 None；MagicMock 默认返回 truthy mock
    # 会让 is_interrupt_recovery_injected 哨兵误判「已注入」
    skill_turbo_session.get_state = MagicMock(return_value=None)
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        MagicMock(return_value=skill_turbo_session),
    )
    clear_artifacts_spy = AsyncMock()
    monkeypatch.setattr(node_artifact_store, "clear_node_artifacts", clear_artifacts_spy)

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={"tui_sess_1": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _session_agent_tasks={},
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)
    # P0 通用中断态清理经 post_agent_execute_for_session 落盘，需可 await 的
    # checkpointer（object.__new__ 构造的 adapter 无 __init__ 属性）
    adapter._checkpointer = SimpleNamespace(post_agent_execute=AsyncMock())

    response = await adapter.process_interrupt(_build_interrupt_request("cancel"))

    # 终止在前、清理在后（消除"清了又被写"竞态）
    assert "clear_hitl" in call_order
    assert call_order.index("cancel_round") < call_order.index("clear_hitl")
    # 待回答的 skill_acceleration_exec tool_call 从上下文尾部弹出，不留悬挂调用
    context.pop_messages.assert_called_once_with(1, with_history=True)
    context_engine.save_contexts.assert_awaited_once_with(loop_session)
    # 清除（含 context 弹出写回）必须 commit 落盘，且晚于 save_contexts
    loop_session.commit.assert_awaited()
    assert call_order.index("save_contexts") < call_order.index("commit")
    # ToolInterruptionState 清空：下一条消息将进入全新 invocation 做意图判断
    assert call({INTERRUPTION_KEY: None}) in loop_session.update_state.call_args_list
    # resume_ctx 经 {card.id}__skill_turbo 隔离键清除（而非 loop_session 的 DeepAgent 键）
    assert call({SKILL_TURBO_RESUME_CTX_KEY: None}) in (
        skill_turbo_session.update_state.call_args_list
    )
    # P0 起取消流有两条 session 生命周期（通用中断态清理 + skill_turbo 隔离键
    # 清理），共享同一 mock：断言至少完整跑过一次 pre/post_run
    assert skill_turbo_session.pre_run.await_count >= 1
    assert skill_turbo_session.post_run.await_count >= 1
    # node_artifacts 保留：供 executor 的 plan_code_hash 匹配（resume 重放继承
    # 同 hash 产物，fresh 清盘）
    clear_artifacts_spy.assert_not_awaited()
    assert response.payload["intent"] == "cancel"
    assert response.payload["success"] is True


@pytest.mark.asyncio
async def test_interaction_supplement_clears_pending_skill_turbo_hitl_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """supplement（新输入顶掉 pending 问卷）同样要清 skill_acceleration_exec HITL 状态。"""
    interruption_state = _skill_turbo_interruption_state()
    loop_session, context_engine, context = _loop_session_fixture(interruption_state)

    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.cancel_round = AsyncMock(return_value=True)
    instance.goal_manager = None
    instance.card = MagicMock(id="card-hitl")

    skill_turbo_session = MagicMock()
    skill_turbo_session.pre_run = AsyncMock()
    skill_turbo_session.post_run = AsyncMock()
    # 真实空 session 的 get_state 返回 None；MagicMock 默认返回 truthy mock
    # 会让 is_interrupt_recovery_injected 哨兵误判「已注入」
    skill_turbo_session.get_state = MagicMock(return_value=None)
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        MagicMock(return_value=skill_turbo_session),
    )

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={"tui_sess_1": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _session_agent_tasks={},
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)

    request = _build_interrupt_request("supplement")
    request.params["new_input"] = "继续执行"
    response = await adapter.process_interrupt(request)

    context.pop_messages.assert_called_once_with(1, with_history=True)
    assert call({INTERRUPTION_KEY: None}) in loop_session.update_state.call_args_list
    assert call({SKILL_TURBO_RESUME_CTX_KEY: None}) in (
        skill_turbo_session.update_state.call_args_list
    )
    assert response.payload["intent"] == "supplement"


@pytest.mark.asyncio
async def test_noninteraction_cancel_clears_hitl_after_teardown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """非交互路径：cancel 的 HITL 清理必须晚于 _stop_session_interrupt_work 终止。

    fresh 执行中被 cancel 时 executor 可能在退出前落盘 resume_ctx，先清会被
    覆盖（"清了又被写"竞态），下一条消息仍命中残留断点重放被取消任务。
    """
    interruption_state = _skill_turbo_interruption_state()
    loop_session, context_engine, context = _loop_session_fixture(interruption_state)

    instance = MagicMock()
    instance._interaction_started = False
    instance._loop_session = loop_session
    instance.react_agent = SimpleNamespace(context_engine=context_engine)
    instance.abort = AsyncMock()
    instance.goal_manager = None
    instance.card = MagicMock(id="card-hitl")

    call_order: list[str] = []
    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    rail.abort = MagicMock(side_effect=lambda *_a, **_kw: call_order.append("rail_abort"))
    def _track_update_state(state: dict) -> None:
        if INTERRUPTION_KEY in state:
            call_order.append("clear_hitl")
    loop_session.update_state = MagicMock(side_effect=_track_update_state)

    skill_turbo_session = MagicMock()
    skill_turbo_session.pre_run = AsyncMock()
    skill_turbo_session.post_run = AsyncMock()
    # 真实空 session 的 get_state 返回 None；MagicMock 默认返回 truthy mock
    # 会让 is_interrupt_recovery_injected 哨兵误判「已注入」
    skill_turbo_session.get_state = MagicMock(return_value=None)
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        MagicMock(return_value=skill_turbo_session),
    )
    monkeypatch.setattr(
        "openjiuwen.core.sys_operation.shell_process_registry.kill_shell_processes_for_session_tree",
        MagicMock(return_value=0),
    )

    adapter = _make_adapter(
        _active_session_ids={"tui_sess_1": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _session_agent_tasks={},
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)
    adapter._cancel_scheduler_running_tasks = MagicMock()

    response = await adapter.process_interrupt(_build_interrupt_request("cancel"))

    # 终止（rail abort）在前、清理在后
    assert "clear_hitl" in call_order
    assert call_order.index("rail_abort") < call_order.index("clear_hitl")
    assert call({INTERRUPTION_KEY: None}) in loop_session.update_state.call_args_list
    assert call({SKILL_TURBO_RESUME_CTX_KEY: None}) in (
        skill_turbo_session.update_state.call_args_list
    )
    assert response.payload["intent"] == "cancel"
    assert response.payload["success"] is True


@pytest.mark.asyncio
async def test_interrupt_cleanup_skips_other_sessions_and_pure_ask_user(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """其它 session / 纯 ask_user interrupt 不触发 skill_turbo 清理。"""
    tool_call = SimpleNamespace(id="call-0", name="ask_user")
    interruption_state = SimpleNamespace(
        ai_message=SimpleNamespace(tool_calls=[tool_call]),
        interrupted_tools={"call-0": SimpleNamespace(tool_call=tool_call)},
    )
    loop_session, _context_engine, context = _loop_session_fixture(interruption_state)

    instance = MagicMock()
    instance._interaction_started = True
    instance._loop_session = loop_session
    instance.cancel_round = AsyncMock(return_value=True)
    instance.goal_manager = None
    instance.card = MagicMock(id="card-hitl")

    create_session_spy = MagicMock(return_value=MagicMock())
    monkeypatch.setattr(
        "openjiuwen.core.session.agent.create_agent_session",
        create_session_spy,
    )

    rail = MagicMock()
    rail.get_cancelled_tool_results.return_value = []
    adapter = _make_adapter(
        _active_session_ids={"tui_sess_1": 1},
        _stream_event_rail=rail,
        _instance=instance,
        _session_agent_tasks={},
    )
    adapter._cancel_pending_todos = AsyncMock(return_value=None)

    await adapter.process_interrupt(_build_interrupt_request("cancel"))

    # P0 起 cancel 流总会跑一次通用中断态清理（独立 session：哨兵 / 终态相位 /
    # resume ctx），与 skill_turbo 无关——此处只创建这一个 session。
    create_session_spy.assert_called_once()
    # 纯 ask_user interrupt 不走 skill_turbo DeepAgent 侧清理：无悬挂 tool_call
    # 弹出、DeepAgent 的 INTERRUPTION_KEY 不经 loop_session 清除
    context.pop_messages.assert_not_called()
    assert call({INTERRUPTION_KEY: None}) not in loop_session.update_state.call_args_list


@pytest.mark.asyncio
async def test_resume_success_clears_deep_agent_hitl_state(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """resume_stream 成功后必须清掉 DeepAgent 侧 pending HITL 状态，否则下一条消息重放中断点重跑任务。"""

    class _FakeTurbo:
        def __init__(self, _config: Any) -> None:
            self.artifact_holder = {}

        async def resume_stream(self, **_kwargs: Any):
            if False:
                yield None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
        _FakeTurbo,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model = None
    adapter.build_skill_turbo_config = lambda: {}  # type: ignore[method-assign]
    adapter._log_and_make_usage_summary_chunk = lambda **_k: None  # type: ignore[method-assign]
    adapter._rewrite_skill_turbo_usage_chunk = lambda chunk, **_k: (chunk, None)  # type: ignore[method-assign]
    clear_hitl = AsyncMock(return_value=True)
    adapter._clear_pending_skill_turbo_hitl = clear_hitl  # type: ignore[method-assign]
    isolated_clear = AsyncMock()
    adapter._clear_skill_turbo_resume_ctx_via_isolated_session = isolated_clear  # type: ignore[method-assign]

    class _Session:
        async def post_run(self) -> None:
            return None

    request = AgentRequest(
        request_id="req-resume-ok",
        channel_id="officeclaw",
        session_id="sess-resume-ok",
        req_method=ReqMethod.CHAT_SEND,
        params={"source": "ask_user_interrupt", "answers": [{"question": "风格"}]},
    )
    stream = adapter._make_skill_turbo_resume_stream(
        request=request,
        inputs={},
        session=_Session(),
        resume_ctx={"plan_code": "x", "pending_tool_call_id": "tc-1", "inputs": {}},
        answers=[{"question": "风格", "selected_options": ["商务经典"]}],
    )
    assert stream is not None
    _ = [chunk async for chunk in stream]

    # DeepAgent 侧 pending HITL 状态被清除（含上下文尾部 tool_call + INTERRUPTION_KEY）
    clear_hitl.assert_awaited_once_with("sess-resume-ok")
    # 命中 _clear_pending_skill_turbo_hitl 时不再走 isolated fallback
    isolated_clear.assert_not_awaited()


@pytest.mark.asyncio
async def test_resume_success_falls_back_to_isolated_clear_when_hitl_miss(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """loop session 不匹配（无 DeepAgent pending）时退化为仅清隔离键 resume_ctx。"""

    class _FakeTurbo:
        def __init__(self, _config: Any) -> None:
            self.artifact_holder = {}

        async def resume_stream(self, **_kwargs: Any):
            if False:
                yield None

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.agent.SkillTurbo",
        _FakeTurbo,
    )

    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._model = None
    adapter.build_skill_turbo_config = lambda: {}  # type: ignore[method-assign]
    adapter._log_and_make_usage_summary_chunk = lambda **_k: None  # type: ignore[method-assign]
    adapter._rewrite_skill_turbo_usage_chunk = lambda chunk, **_k: (chunk, None)  # type: ignore[method-assign]
    adapter._clear_pending_skill_turbo_hitl = AsyncMock(return_value=False)  # type: ignore[method-assign]
    isolated_clear = AsyncMock()
    adapter._clear_skill_turbo_resume_ctx_via_isolated_session = isolated_clear  # type: ignore[method-assign]

    class _Session:
        async def post_run(self) -> None:
            return None

    request = AgentRequest(
        request_id="req-resume-miss",
        channel_id="officeclaw",
        session_id="sess-resume-miss",
        req_method=ReqMethod.CHAT_SEND,
        params={"source": "ask_user_interrupt", "answers": [{"question": "风格"}]},
    )
    stream = adapter._make_skill_turbo_resume_stream(
        request=request,
        inputs={},
        session=_Session(),
        resume_ctx={"plan_code": "x", "pending_tool_call_id": "tc-1", "inputs": {}},
        answers=[{"question": "风格", "selected_options": ["商务经典"]}],
    )
    assert stream is not None
    _ = [chunk async for chunk in stream]

    isolated_clear.assert_awaited_once_with("sess-resume-miss")


# ────────────────── P2-2: resume 重放按 plan_code 继承 node_artifacts ──────────────────

_PLAN_CODE = "root = PlanNode(name='p', description='d')"
_PLAN_HASH = executor_module.SkillTurboExecutor._hash_code(_PLAN_CODE)


def _make_replay_executor() -> executor_module.SkillTurboExecutor:
    """构造带最小属性集的 executor（绕过 __init__，对齐 clear 测试范式）。"""
    executor = object.__new__(executor_module.SkillTurboExecutor)
    executor._env = SimpleNamespace(card=MagicMock(), skill_name="ppt")
    executor._node_artifacts_holder = {}
    executor._current_plan_code = _PLAN_CODE
    # _persist_node_artifacts 产物溯源：inputs 无 skill_name 时回退 env 值。
    executor._execution_inputs = {}
    return executor


def _replay_session_mock() -> MagicMock:
    load_session = MagicMock()
    load_session.session_id = "sess-replay"
    load_session.pre_run = AsyncMock()
    load_session.post_run = AsyncMock()
    return load_session


@pytest.mark.asyncio
async def test_resume_replay_inherits_matching_plan_artifacts() -> None:
    """resume 重放必须把同 plan_code 的已完成节点产物继承进新 executor holder。

    复现 P2-2 缺口：每请求新建 executor，holder 为空 + completed 阶段被跳过
    不再采集 → finally 落盘用「空底+新增」覆盖 session，中断前已完成阶段的
    产物丢失。
    """
    executor = _make_replay_executor()
    load_session = _replay_session_mock()
    persisted_nodes = {
        "p1_outline": {"status": "completed", "files": [{"path": "a.md"}]},
        "p2_content": {"status": "completed"},
    }
    with (
        patch.object(
            executor_module, "create_agent_session", return_value=load_session
        ),
        patch.object(executor_module, "set_skill_turbo_id", MagicMock()),
        patch.object(
            executor_module,
            "load_node_artifacts",
            AsyncMock(
                return_value={
                    "skill": "ppt",
                    "plan_code_hash": _PLAN_HASH,
                    "nodes": persisted_nodes,
                }
            ),
        ),
    ):
        token = executor_module._session_var.set(
            SimpleNamespace(session_id="sess-replay")
        )
        try:
            await executor._inherit_node_artifacts_for_replay(_PLAN_CODE)
        finally:
            executor_module._session_var.reset(token)

    assert executor._node_artifacts_holder == persisted_nodes
    load_session.pre_run.assert_awaited_once()
    load_session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_replay_skips_inherit_on_hash_mismatch() -> None:
    """plan_code 哈希不一致（planner 重新生成 plan）→ 不继承，holder 保持为空。"""
    executor = _make_replay_executor()
    load_session = _replay_session_mock()
    with (
        patch.object(
            executor_module, "create_agent_session", return_value=load_session
        ),
        patch.object(executor_module, "set_skill_turbo_id", MagicMock()),
        patch.object(
            executor_module,
            "load_node_artifacts",
            AsyncMock(
                return_value={
                    "skill": "ppt",
                    "plan_code_hash": "deadbeef",
                    "nodes": {"p1_outline": {"status": "completed"}},
                }
            ),
        ),
    ):
        token = executor_module._session_var.set(
            SimpleNamespace(session_id="sess-replay")
        )
        try:
            await executor._inherit_node_artifacts_for_replay(_PLAN_CODE)
        finally:
            executor_module._session_var.reset(token)

    assert executor._node_artifacts_holder == {}


@pytest.mark.asyncio
async def test_resume_replay_inherits_legacy_records_without_hash() -> None:
    """旧记录无 plan_code_hash 字段 → 视为匹配（fail-open 向后兼容）。"""
    executor = _make_replay_executor()
    load_session = _replay_session_mock()
    legacy_nodes = {"p1_outline": {"status": "completed"}}
    with (
        patch.object(
            executor_module, "create_agent_session", return_value=load_session
        ),
        patch.object(executor_module, "set_skill_turbo_id", MagicMock()),
        patch.object(
            executor_module,
            "load_node_artifacts",
            AsyncMock(
                return_value={"skill": "ppt", "nodes": legacy_nodes}
            ),
        ),
    ):
        token = executor_module._session_var.set(
            SimpleNamespace(session_id="sess-replay")
        )
        try:
            await executor._inherit_node_artifacts_for_replay(_PLAN_CODE)
        finally:
            executor_module._session_var.reset(token)

    assert executor._node_artifacts_holder == legacy_nodes


@pytest.mark.asyncio
async def test_resume_replay_inherit_failopen_on_error() -> None:
    """加载抛异常 → 不上抛（fail-open），holder 保持为空，post_run 仍执行。"""
    executor = _make_replay_executor()
    load_session = _replay_session_mock()
    with (
        patch.object(
            executor_module, "create_agent_session", return_value=load_session
        ),
        patch.object(executor_module, "set_skill_turbo_id", MagicMock()),
        patch.object(
            executor_module,
            "load_node_artifacts",
            AsyncMock(side_effect=RuntimeError("checkpointer down")),
        ),
    ):
        token = executor_module._session_var.set(
            SimpleNamespace(session_id="sess-replay")
        )
        try:
            await executor._inherit_node_artifacts_for_replay(_PLAN_CODE)
        finally:
            executor_module._session_var.reset(token)

    assert executor._node_artifacts_holder == {}
    load_session.post_run.assert_awaited_once()


@pytest.mark.asyncio
async def test_resume_replay_inherit_noop_without_records() -> None:
    """无产物记录 → no-op，holder 保持为空。"""
    executor = _make_replay_executor()
    load_session = _replay_session_mock()
    with (
        patch.object(
            executor_module, "create_agent_session", return_value=load_session
        ),
        patch.object(executor_module, "set_skill_turbo_id", MagicMock()),
        patch.object(
            executor_module, "load_node_artifacts", AsyncMock(return_value=None)
        ),
    ):
        token = executor_module._session_var.set(
            SimpleNamespace(session_id="sess-replay")
        )
        try:
            await executor._inherit_node_artifacts_for_replay(_PLAN_CODE)
        finally:
            executor_module._session_var.reset(token)

    assert executor._node_artifacts_holder == {}


@pytest.mark.asyncio
async def test_persist_node_artifacts_writes_plan_code_hash() -> None:
    """落盘必须写入当前 plan_code 哈希，供 resume 重放继承时比对。"""
    executor = _make_replay_executor()
    executor._node_artifacts_holder = {"p3_export": {"status": "completed"}}
    save_spy = AsyncMock()
    with patch.object(executor_module, "save_node_artifacts", save_spy):
        await executor._persist_node_artifacts(SimpleNamespace())

    save_spy.assert_awaited_once()
    kwargs = save_spy.await_args.kwargs
    assert kwargs["plan_code_hash"] == _PLAN_HASH
    assert kwargs["skill"] == "ppt"
    assert kwargs["nodes"] == {"p3_export": {"status": "completed"}}
