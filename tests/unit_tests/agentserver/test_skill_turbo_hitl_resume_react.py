# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""HITL resume should re-invoke skill_acceleration_exec so ReAct can summarize."""

from __future__ import annotations

import copy
import re
from collections import defaultdict
from types import SimpleNamespace

import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, ToolMessage, UserMessage
from openjiuwen.core.session.interaction.interactive_input import InteractiveInput
from openjiuwen.core.single_agent.interrupt.exception import ToolInterruptException
from openjiuwen.core.single_agent.interrupt.response import ToolCallInterruptRequest
from openjiuwen.core.single_agent.interrupt.state import RESUME_USER_INPUT_KEY
from openjiuwen.core.single_agent.rail.base import ToolCallInputs
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
    _SKILL_TURBO_MODEL_IDENTITY_KEY,
)
from jiuwenswarm.server.runtime.skill_turbo.permission_bridge import (
    SKILL_TURBO_RESUME_CTX_KEY,
    clear_resume_ctx,
    load_resume_ctx,
    mark_resume_in_flight,
    save_resume_ctx,
)
from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
    _SKILL_TURBO_HITL_PLACEHOLDER,
    _SKILL_TURBO_STOP_HINT_NEUTRAL,
    _resolve_skill_turbo_resume_session_id,
    _resume_user_input_from_raw,
    get_skill_turbo_resume_answers,
    reset_skill_turbo_resume_answers,
    set_skill_turbo_hitl_tic,
    set_skill_turbo_resume_answers,
)

# 旧版停止提示文案（宣称文件已发送），生产代码已删除常量；保留字符串仅用于
# 验证存量会话历史（tool_result 嵌旧文案）重放时被 _fix_incomplete_tool_context
# 原样保留——该修复逻辑按 tool_call_id 结构匹配，不依赖常量存在。
_LEGACY_STOP_HINT_TEXT = (
    "\n\n[SYSTEM] The skill_acceleration_exec task is complete and the artifact has already been "
    "generated. The file(s) have ALREADY been sent to the user by the internal "
    "delivery pipeline - do NOT call send_file_to_user again. You should now "
    "summarize this result to the user and finish your turn. Do NOT call "
    "skill_acceleration_exec, skill_tool, or send_file_to_user again for this task - the "
    "work is already done; calling any of them again would duplicate the work."
)


class _StreamSession:
    def __init__(self):
        self.chunks = []

    async def write_stream(self, chunk):
        self.chunks.append(chunk)


def _tool_ctx(session, tool_name: str):
    from openjiuwen.core.single_agent.rail.base import ToolCallInputs

    tool_call = SimpleNamespace(id="call-1", name=tool_name, arguments={})
    return SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name=tool_name,
            tool_args={},
            tool_result={"success": True},
        ),
        extra={},
        exception=None,
    )


async def _async_none():
    return None


@pytest.mark.asyncio
async def test_process_stream_does_not_persist_ambiguous_model_identity(monkeypatch):
    """A model_ref with multiple owners must not enter a resumable request."""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._instance = SimpleNamespace()
    adapter._deepresearch_rewrite_tx_uncertain = False
    adapter._active_session_ids = defaultdict(int)
    adapter._session_agent_tasks = {}
    model = SimpleNamespace(model_config=SimpleNamespace(model_name="glm-5.2"))
    other_model = SimpleNamespace(model_config=SimpleNamespace(model_name="glm-5.2"))
    adapter._model_cache = {
        "glm-5.2#0": model,
        "glm-5.2#1": other_model,
    }
    adapter._model_identity_to_keys = {
        "model-ref-1": ["glm-5.2#0", "glm-5.2#1"]
    }
    adapter._resolve_model_for_request = lambda _request: model  # type: ignore[method-assign]
    adapter._apply_model_to_react_agent = lambda _model: None  # type: ignore[method-assign]
    adapter._has_valid_model_config = lambda _model_name: True  # type: ignore[method-assign]
    adapter._maybe_apply_pending_reload = lambda: _async_none()  # type: ignore[method-assign]
    adapter._bind_runtime_cron_context = lambda **_kwargs: None  # type: ignore[method-assign]
    adapter._reset_stream_runtime_context = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    adapter._run_cleanup_steps = lambda _steps: None  # type: ignore[method-assign]
    adapter._register_session_agent_task = lambda _session_id: None  # type: ignore[method-assign]
    adapter._unregister_session_agent_task = lambda _session_id: None  # type: ignore[method-assign]
    adapter._runtime_cron_tool_context = SimpleNamespace(
        remember_current_binding=lambda: None
    )
    adapter._config_base_cache = {}
    adapter._refresh_standard_agent_permissions_body = lambda _request: None  # type: ignore[method-assign]
    adapter._bind_agent_permissions_base = lambda: None  # type: ignore[method-assign]
    adapter._update_permission_rail = lambda *_args, **_kwargs: None  # type: ignore[method-assign]
    adapter._is_subagent_approval_answer = lambda *_args: False  # type: ignore[method-assign]
    adapter._refresh_stale_mcp_tool_lists = lambda: _async_none()  # type: ignore[method-assign]
    adapter._inject_extension_config_into_inputs = lambda *_args: None  # type: ignore[method-assign]
    adapter._inject_model_selection_into_inputs = lambda *_args: None  # type: ignore[method-assign]
    adapter._deepresearch_artifact_output_dir = lambda _path: ""  # type: ignore[method-assign]
    adapter._wants_attach_goal = lambda _params: False  # type: ignore[method-assign]
    adapter._structured_goal_op_from_request = lambda _request: None  # type: ignore[method-assign]
    adapter._should_parse_tui_goal_slash = lambda **_kwargs: False  # type: ignore[method-assign]
    for _moved_name in (
        "_run_cleanup_steps",
        "_is_subagent_approval_answer",
        "_inject_model_selection_into_inputs",
        "_wants_attach_goal",
        "_structured_goal_op_from_request",
        "_should_parse_tui_goal_slash",
    ):
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.agent_adapter.interface_deep." + _moved_name,
            adapter.__dict__[_moved_name],
        )
    adapter._handle_slash_command = lambda *_args, **_kwargs: _async_none()  # type: ignore[method-assign]
    captured_metadata = None

    class _StopAfterRuntimeConfig(BaseException):
        pass

    async def capture_runtime_config(runtime_config):
        nonlocal captured_metadata
        captured_metadata = runtime_config.request_metadata
        raise _StopAfterRuntimeConfig

    adapter._update_runtime_config = capture_runtime_config  # type: ignore[method-assign]
    adapter._persist_session_checkpoint = lambda *_args: _async_none()  # type: ignore[method-assign]
    adapter._revoke_main_skill_authorization = lambda *_args, **_kwargs: None  # type: ignore[method-assign]

    request = AgentRequest(
        request_id="req-model-identity",
        channel_id="test-channel",
        session_id="sess-model-identity",
        req_method=ReqMethod.CHAT_SEND,
        params={"query": "生成PPT", "mode": "agent"},
    )

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.setup_permission_context",
        lambda _request: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.setup_permissions_session_scope",
        lambda _session_id: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep.is_enterprise",
        lambda: True,
    )
    with pytest.raises(_StopAfterRuntimeConfig):
        async for _chunk in adapter._process_message_stream_impl(request, {}):
            pass

    assert captured_metadata is not None
    assert _SKILL_TURBO_MODEL_IDENTITY_KEY not in captured_metadata

@pytest.mark.asyncio
async def test_mark_resume_in_flight_persists_across_sessions():
    """in-flight flag must survive post_run so a second session load sees it."""
    checkpoint: dict = {}

    class _CheckpointSession:
        def __init__(self):
            self._state: dict = {}

        async def pre_run(self, inputs=None):
            self._state = copy.deepcopy(checkpoint)

        async def post_run(self):
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))

        def update_state(self, mapping):
            self._state.update(mapping)

        def get_state(self, key):
            return self._state.get(key)

    ctx = {
        "plan_code": "plan-x",
        "pending_tool_call_id": "skill_turbo-tc-ask_user-1",
        "inputs": {},
    }
    writer = _CheckpointSession()
    await writer.pre_run()
    writer.update_state({SKILL_TURBO_RESUME_CTX_KEY: ctx})
    await writer.post_run()

    marker = _CheckpointSession()
    loaded = await load_resume_ctx(marker)
    assert loaded is not None
    assert loaded.get("resume_in_flight") is not True
    await mark_resume_in_flight(marker, loaded)

    reader = _CheckpointSession()
    again = await load_resume_ctx(reader)
    assert again is not None
    assert again.get("resume_in_flight") is True
    assert again.get("pending_tool_call_id") == "skill_turbo-tc-ask_user-1"


@pytest.mark.asyncio
async def test_save_resume_ctx_clears_stale_resume_in_flight():
    """嵌套 ask_user 中断后 save_resume_ctx 必须清除上一轮 mark_resume_in_flight 残留标志。

    场景：第一次 ask_user 中断→恢复时 mark_resume_in_flight 设置 resume_in_flight=True；
    恢复过程中第二次 ask_user 中断→save_resume_ctx 保存新 resume_ctx。
    openjiuwen session.update_state 使用 merge 语义（update_dict），新 entry 缺少
    resume_in_flight 键不会触发删除。修复后 save_resume_ctx 显式置 None 让 merge
    将其从持久化状态中移除，下一轮 load_resume_ctx 看不到残留标志。
    """
    from openjiuwen.core.session.utils import update_dict

    checkpoint: dict = {}

    class _MergeCheckpointSession:
        """模拟 openjiuwen session + checkpointer 的 merge 语义。

        update_state 走 update_dict（与真实 InMemoryStateLike.update 一致）：
        值为 None 的键会被删除，而不是保留。
        """

        def __init__(self):
            self._state: dict = {}

        async def pre_run(self, inputs=None):
            self._state = copy.deepcopy(checkpoint)

        async def post_run(self):
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))

        def update_state(self, mapping):
            update_dict(mapping, self._state)

        def get_state(self, key):
            return copy.deepcopy(self._state.get(key))

    # 第一次中断：保存 resume_ctx（无 resume_in_flight）
    writer1 = _MergeCheckpointSession()
    await writer1.pre_run()
    await save_resume_ctx(
        writer1,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-1",
        task_states=None,
    )
    await writer1.post_run()

    # 第一次恢复：mark_resume_in_flight 设置 resume_in_flight=True
    marker = _MergeCheckpointSession()
    loaded = await load_resume_ctx(marker)
    assert loaded is not None
    assert loaded.get("resume_in_flight") is not True
    await mark_resume_in_flight(marker, loaded)

    # 第二次中断：save_resume_ctx 保存新 resume_ctx（新 tcid）
    writer2 = _MergeCheckpointSession()
    await save_resume_ctx(
        writer2,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-2",
        task_states=None,
    )
    await writer2.post_run()

    # 下一轮恢复加载：resume_in_flight 必须为 False
    reader = _MergeCheckpointSession()
    again = await load_resume_ctx(reader)
    assert again is not None
    assert again.get("pending_tool_call_id") == "skill_turbo-tc-ask_user-2"
    assert not again.get("resume_in_flight"), (
        "save_resume_ctx 必须清除上一轮 mark_resume_in_flight 残留的 resume_in_flight 标志"
    )


@pytest.mark.asyncio
async def test_save_resume_ctx_persists_outer_task_id():
    """HITL resume checkpoint keeps the outer Todo id for finalization."""
    checkpoint: dict = {}

    class _CheckpointSession:
        def __init__(self):
            self._state: dict = {}

        async def pre_run(self, inputs=None):
            self._state = copy.deepcopy(checkpoint)

        async def post_run(self):
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))

        def update_state(self, mapping):
            self._state.update(mapping)

        def get_state(self, key):
            return copy.deepcopy(self._state.get(key))

    writer = _CheckpointSession()
    await save_resume_ctx(
        writer,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-outer-task",
        task_states=None,
        outer_task_id="todo:create_ppt",
    )
    await writer.post_run()

    reader = _CheckpointSession()
    loaded = await load_resume_ctx(reader)
    assert loaded is not None
    assert loaded["outer_task_id"] == "create_ppt"


@pytest.mark.asyncio
async def test_save_resume_ctx_preserves_outer_task_id_across_nested_interrupt():
    """A second HITL interruption keeps the first interruption's outer Todo id."""
    from openjiuwen.core.session.utils import update_dict

    checkpoint: dict = {}

    class _MergeCheckpointSession:
        def __init__(self):
            self._state: dict = {}

        async def pre_run(self, inputs=None):
            self._state = copy.deepcopy(checkpoint)

        async def post_run(self):
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))

        def update_state(self, mapping):
            update_dict(mapping, self._state)

        def get_state(self, key):
            return copy.deepcopy(self._state.get(key))

    first_interrupt = _MergeCheckpointSession()
    await save_resume_ctx(
        first_interrupt,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-1",
        task_states=None,
        outer_task_id="todo:create_ppt",
    )
    await first_interrupt.post_run()

    first_resume = _MergeCheckpointSession()
    first_ctx = await load_resume_ctx(first_resume)
    assert first_ctx is not None
    assert first_ctx["outer_task_id"] == "create_ppt"
    await mark_resume_in_flight(first_resume, first_ctx)

    second_interrupt = _MergeCheckpointSession()
    await save_resume_ctx(
        second_interrupt,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-2",
        task_states=None,
    )
    await second_interrupt.post_run()

    second_resume = _MergeCheckpointSession()
    second_ctx = await load_resume_ctx(second_resume)
    assert second_ctx is not None
    assert second_ctx["pending_tool_call_id"] == "skill_turbo-tc-ask_user-2"
    assert second_ctx["outer_task_id"] == "create_ppt"


@pytest.mark.asyncio
async def test_clear_resume_ctx_persists_across_sessions():
    """clear_resume_ctx 后新 session load_resume_ctx 必须返回 None。

    场景：SkillTurbo resume 成功交付 PPT 后调用 clear_resume_ctx，随后同一
    session_id 的新请求（如过期的 permission_interrupt 续延）经
    skill_acceleration_exec 工具再次 load_resume_ctx。若 clear 未真正落盘，
    新 session pre_run 会从 checkpointer 读回残留 ctx，触发 PPT 重复生成。

    openjiuwen Session.post_run 是幂等的（_post_run_done 标志）。
    _try_skill_turbo_resume 路径里 mark_resume_in_flight 已 post_run 过，
    随后 _clear_resume_ctx() 若用同一 session 再 post_run 是 no-op，不 commit。
    修复后 _clear_resume_ctx() 改用独立 session（pre_run+clear+post_run）保证落盘。
    本测试模拟该不变量：clear 后任意新 session load 必须返回 None。
    """
    from openjiuwen.core.session.utils import update_dict

    checkpoint: dict = {}

    class _MergeCheckpointSession:
        """模拟 openjiuwen Session 的 merge 语义 + 幂等 pre_run/post_run。"""

        def __init__(self):
            self._state: dict = {}
            self._post_run_done = False
            self._pre_run_done = False

        async def pre_run(self, inputs=None):
            if self._pre_run_done:
                return
            self._state = copy.deepcopy(checkpoint)
            self._pre_run_done = True
            self._post_run_done = False

        async def post_run(self):
            if self._post_run_done:
                return
            checkpoint.clear()
            checkpoint.update(copy.deepcopy(self._state))
            self._post_run_done = True

        def update_state(self, mapping):
            update_dict(mapping, self._state)

        def get_state(self, key):
            return copy.deepcopy(self._state.get(key))

    # 中断：保存 resume_ctx
    saver = _MergeCheckpointSession()
    await saver.pre_run()
    await save_resume_ctx(
        saver,
        plan_code="plan-x",
        inputs={},
        pending_tool_call_id="skill_turbo-tc-ask_user-1",
        task_states=None,
    )
    await saver.post_run()

    # 恢复请求到达：_try_skill_turbo_resume 创建新 session
    resume_session = _MergeCheckpointSession()
    await resume_session.pre_run()
    loaded = await load_resume_ctx(resume_session)
    assert loaded is not None
    # mark_resume_in_flight: pre_run + update_state + post_run（已 post_run）
    await mark_resume_in_flight(resume_session, loaded)

    # 恢复成功后 clear：模拟修复后的 _clear_resume_ctx() —— 用独立 session
    # （同一 session 的 post_run 已被 mark_resume_in_flight 触发，是 no-op）。
    clear_session = _MergeCheckpointSession()
    await clear_session.pre_run()
    await clear_resume_ctx(clear_session)
    await clear_session.post_run()

    # 新请求新 session：skill_acceleration_exec 工具 load_resume_ctx
    reader = _MergeCheckpointSession()
    stale = await load_resume_ctx(reader)
    assert stale is None, (
        "clear_resume_ctx 后 load_resume_ctx 必须返回 None；"
        "残留 ctx 会让过期中断续延触发任务重复执行"
    )


@pytest.mark.asyncio
async def test_isolated_clear_prevents_post_run_revival_with_real_session():
    """真实 Session + 真实 checkpointer：isolated clear 后调用方 post_run 不得复活条目。

    复现 R1 回归：checkpointer post_run 是全量状态覆盖（非增量合并），
    clear() 若只清磁盘、不同步清调用方 session 的内存快照，随后
    turbo_session.post_run() 会把含旧条目的内存快照整体写回——
    resume_ctx 复活、下一次调用重放已完成任务。
    """
    from openjiuwen.core.session.agent import create_agent_session
    from jiuwenswarm.server.runtime.skill_turbo.resume_context import (
        ResumeContextManager,
        SKILL_TURBO_RESUME_CTX_KEY,
        set_skill_turbo_id,
    )

    class _Card:
        id = "card-revival-guard"

    def _mk_session():
        s = create_agent_session(session_id="sess-revival", card=_Card())
        set_skill_turbo_id(s, _Card())
        return s

    # 中断：落盘 resume_ctx 条目
    saver = _mk_session()
    await saver.pre_run()
    saver.update_state(
        {SKILL_TURBO_RESUME_CTX_KEY: {"plan_code": "plan-x", "pending_tool_call_id": "tc-1"}}
    )
    await saver.post_run()

    # resume 流程：turbo_session pre_run（内存快照含条目）→ isolated clear
    turbo = _mk_session()
    await turbo.pre_run()
    assert turbo.get_state(SKILL_TURBO_RESUME_CTX_KEY) is not None

    mgr = ResumeContextManager(turbo)
    await mgr.clear()

    # 磁盘（隔离键空间）已清
    checker = _mk_session()
    await checker.pre_run()
    assert checker.get_state(SKILL_TURBO_RESUME_CTX_KEY) is None, (
        "isolated clear 后隔离键空间不应有 resume_ctx 条目"
    )

    # 调用方 session post_run（全量写回）不得复活条目
    await turbo.post_run()
    reader = _mk_session()
    await reader.pre_run()
    revived = reader.get_state(SKILL_TURBO_RESUME_CTX_KEY)
    assert revived is None, (
        f"turbo_session.post_run 全量写回复活了 resume_ctx（{revived}）；"
        "clear() 必须同步清调用方内存快照，否则 resume 跑通后断点残留、"
        "下一次调用重放已完成任务（R1 回归）"
    )


def test_resume_user_input_from_interactive_input():
    interactive = InteractiveInput()
    payload = {
        "status": "answered",
        "answers": [
            {"question": "受众", "selected_options": ["企业高管"]},
            {"question": "目的", "selected_options": ["工作汇报"]},
        ],
    }
    interactive.update("call_outer", payload)
    got = _resume_user_input_from_raw(interactive, {}, None)
    assert got is payload
    assert len(got["answers"]) == 2


def test_resume_user_input_from_raw_answers_list():
    answers = [{"question": "页数", "selected_options": ["10"]}]
    adapter = SimpleNamespace(
        _skill_turbo_answers_to_confirm_payload=lambda raw, _ctx: raw
    )
    assert _resume_user_input_from_raw(answers, {}, adapter) == answers


def test_resume_answers_contextvar_roundtrip():
    token = set_skill_turbo_resume_answers(["a"])
    try:
        assert get_skill_turbo_resume_answers() == ["a"]
    finally:
        reset_skill_turbo_resume_answers(token)
    assert get_skill_turbo_resume_answers() is None


def test_resolve_skill_turbo_resume_session_id_prefers_metadata():
    parent = SimpleNamespace(get_session_id=lambda: "parent-sid")
    assert _resolve_skill_turbo_resume_session_id("meta-sid", parent) == "meta-sid"


def test_resolve_skill_turbo_resume_session_id_falls_back_to_parent():
    parent = SimpleNamespace(get_session_id=lambda: "parent-sid")
    assert _resolve_skill_turbo_resume_session_id("", parent) == "parent-sid"
    assert _resolve_skill_turbo_resume_session_id(None, parent) == "parent-sid"
    assert _resolve_skill_turbo_resume_session_id("", None) == ""


@pytest.mark.asyncio
async def test_skill_acceleration_exec_resume_skips_duplicate_tool_call_emit():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _tool_ctx(session, "skill_acceleration_exec")
    ctx.extra[RESUME_USER_INPUT_KEY] = [{"question": "q", "selected_options": ["a"]}]
    await rail.before_tool_call(ctx)
    assert not any(getattr(chunk, "type", None) == "tool_call" for chunk in session.chunks)
    assert not any(getattr(chunk, "type", None) == "tool_update" for chunk in session.chunks)
    await rail.after_tool_call(ctx)


@pytest.mark.asyncio
async def test_skill_acceleration_exec_first_invoke_still_emits_tool_call():
    rail = JiuSwarmStreamEventRail()
    session = _StreamSession()
    ctx = _tool_ctx(session, "skill_acceleration_exec")
    await rail.before_tool_call(ctx)
    assert any(getattr(chunk, "type", None) == "tool_call" for chunk in session.chunks)
    await rail.after_tool_call(ctx)


def test_hitl_placeholder_is_recognized_by_context_repair():
    rail = JiuSwarmStreamEventRail()
    tool_call_id = "call_982c"
    placeholders = {
        tool_call_id: rail._tool_interrupted_message("skill_acceleration_exec"),
    }
    names = {tool_call_id: "skill_acceleration_exec"}
    leaked = ToolMessage(
        content="{'success': False, 'error': '任务已暂停等待审批'}",
        tool_call_id=tool_call_id,
    )
    placeholder = ToolMessage(
        content=_SKILL_TURBO_HITL_PLACEHOLDER,
        tool_call_id=tool_call_id,
    )
    assert rail._is_tool_interrupt_placeholder(leaked, placeholders, names) is False
    assert rail._is_tool_interrupt_placeholder(placeholder, placeholders, names) is True


@pytest.mark.asyncio
async def test_skill_turbo_hitl_after_tool_call_writes_placeholder_tool_msg():
    rail = JiuSwarmStreamEventRail()
    rail.set_skill_turbo_adapter(object())
    session = _StreamSession()
    tool_call = SimpleNamespace(
        id="call_982c",
        name="skill_acceleration_exec",
        arguments={"query": "生成PPT"},
    )
    leaked = ToolMessage(
        content="{'success': False, 'error': '任务已暂停等待审批'}",
        tool_call_id="call_982c",
    )
    ctx = SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_acceleration_exec",
            tool_args={"query": "生成PPT"},
            tool_result={"success": False, "error": "任务已暂停等待审批"},
            tool_msg=leaked,
        ),
        extra={},
        exception=None,
        request_force_finish=lambda *_args, **_kwargs: None,
    )
    inner_tc = SimpleNamespace(
        id="skill_turbo-tc-ask_user-1",
        name="ask_user",
        arguments={"questions": [{"question": "风格", "options": [{"label": "科技极简"}]}]},
    )
    tic = SimpleNamespace(
        request=SimpleNamespace(message="ask", tool_call_id="skill_turbo-tc-ask_user-1"),
        tool_call=inner_tc,
    )
    set_skill_turbo_hitl_tic(tic)
    try:
        await rail.after_tool_call(ctx)
    finally:
        set_skill_turbo_hitl_tic(None)

    assert isinstance(ctx.inputs.tool_result, ToolInterruptException)
    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.content == _SKILL_TURBO_HITL_PLACEHOLDER
    assert ctx.inputs.tool_msg.tool_call_id == "call_982c"
    assert ctx.inputs.tool_msg is not leaked


@pytest.mark.asyncio
async def test_skill_turbo_hitl_after_tool_call_rewrites_from_result_marker():
    from jiuwenswarm.server.runtime.skill_turbo.skill_turbo_tools import (
        build_skill_turbo_hitl_result,
    )

    rail = JiuSwarmStreamEventRail()
    rail.set_skill_turbo_adapter(object())
    session = _StreamSession()
    tool_call = SimpleNamespace(
        id="call_982d",
        name="skill_acceleration_exec",
        arguments={"query": "生成PPT"},
    )
    ctx = SimpleNamespace(
        session=session,
        inputs=ToolCallInputs(
            tool_call=tool_call,
            tool_name="skill_acceleration_exec",
            tool_args={"query": "生成PPT"},
            tool_result=build_skill_turbo_hitl_result(
                ToolInterruptException(
                    request=ToolCallInterruptRequest(
                        message="风格",
                        auto_confirm_key="ask_user:style",
                    ),
                    tool_call=SimpleNamespace(
                        id="skill_turbo-tc-ask_user-2",
                        name="ask_user",
                        arguments={},
                    ),
                )
            ),
            tool_msg=None,
        ),
        extra={},
        exception=None,
        request_force_finish=lambda *_args, **_kwargs: None,
    )
    set_skill_turbo_hitl_tic(None)
    await rail.after_tool_call(ctx)

    assert isinstance(ctx.inputs.tool_result, ToolInterruptException)
    rebuilt_request = ctx.inputs.tool_result.request
    assert getattr(rebuilt_request, "message", "") == "风格"
    assert getattr(rebuilt_request, "auto_confirm_key", "") == "ask_user:style"
    assert ctx.inputs.tool_result.tool_call is tool_call
    assert isinstance(ctx.inputs.tool_msg, ToolMessage)
    assert ctx.inputs.tool_msg.content == _SKILL_TURBO_HITL_PLACEHOLDER
    assert ctx.inputs.tool_msg.tool_call_id == "call_982d"


class _ModelContext:
    def __init__(self, messages):
        self.messages = list(messages)

    def get_messages(self):
        return list(self.messages)

    def pop_messages(self, size):
        popped = self.messages[:size]
        self.messages = self.messages[size:]
        return popped

    async def add_messages(self, message):
        self.messages.append(message)


@pytest.mark.asyncio
async def test_fix_incomplete_tool_context_keeps_stop_hint_over_hitl_placeholder():
    rail = JiuSwarmStreamEventRail()
    tool_call_id = "call_982c"
    stop_hint_msg = ToolMessage(
        content="任务已完成" + _LEGACY_STOP_HINT_TEXT,
        tool_call_id=tool_call_id,
    )
    ctx = SimpleNamespace(
        context=_ModelContext([
            UserMessage(content="生成一页PPT"),
            AssistantMessage(
                content="",
                tool_calls=[{
                    "type": "function",
                    "id": tool_call_id,
                    "function": {
                        "name": "skill_acceleration_exec",
                        "arguments": "{\"query\":\"生成PPT\"}",
                    },
                }],
            ),
            ToolMessage(
                content=_SKILL_TURBO_HITL_PLACEHOLDER,
                tool_call_id=tool_call_id,
            ),
            ToolMessage(
                content=_SKILL_TURBO_HITL_PLACEHOLDER,
                tool_call_id=tool_call_id,
            ),
            stop_hint_msg,
        ]),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )

    await rail._fix_incomplete_tool_context(ctx)

    messages = ctx.context.get_messages()
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == tool_call_id
    assert "任务已暂停等待审批" not in tool_msgs[0].content
    assert _SKILL_TURBO_HITL_PLACEHOLDER not in tool_msgs[0].content
    assert "The skill_acceleration_exec task is complete" in tool_msgs[0].content
    assert "skill_tool" in tool_msgs[0].content


@pytest.mark.asyncio
async def test_fix_incomplete_tool_context_keeps_neutral_stop_hint_over_hitl_placeholder():
    # 新版中立收尾与旧版停止提示走同一保留路径：
    # 中断 placeholder 被真实 tool_result（含中立收尾）原位替换。
    rail = JiuSwarmStreamEventRail()
    tool_call_id = "call_982d"
    neutral_msg = ToolMessage(
        content="任务已完成" + _SKILL_TURBO_STOP_HINT_NEUTRAL["en"],
        tool_call_id=tool_call_id,
    )
    ctx = SimpleNamespace(
        context=_ModelContext([
            UserMessage(content="生成一页PPT"),
            AssistantMessage(
                content="",
                tool_calls=[{
                    "type": "function",
                    "id": tool_call_id,
                    "function": {
                        "name": "skill_acceleration_exec",
                        "arguments": "{\"query\":\"生成PPT\"}",
                    },
                }],
            ),
            ToolMessage(
                content=_SKILL_TURBO_HITL_PLACEHOLDER,
                tool_call_id=tool_call_id,
            ),
            neutral_msg,
        ]),
        inputs=SimpleNamespace(tools=[]),
        session=None,
        extra={},
    )

    await rail._fix_incomplete_tool_context(ctx)

    messages = ctx.context.get_messages()
    tool_msgs = [m for m in messages if isinstance(m, ToolMessage)]
    assert len(tool_msgs) == 1
    assert tool_msgs[0].tool_call_id == tool_call_id
    assert _SKILL_TURBO_HITL_PLACEHOLDER not in tool_msgs[0].content
    assert "did NOT confirm" in tool_msgs[0].content
    assert "send_file_to_user" in tool_msgs[0].content


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("params", "expect_reload"),
    [
        (
            {
                "query": "resume",
                "answers": [{"question": "页数", "selected_options": ["10"]}],
                "source": "ask_user_interrupt",
            },
            True,
        ),
        ({"query": "生成PPT", "mode": "agent"}, False),
    ],
)
async def test_process_stream_applies_pending_reload_for_skill_turbo_resume(
    params, expect_reload, monkeypatch
):
    """skill_turbo resume 请求必须先应用 pending reload（配置重载先于 resume 恢复）。

    恢复 Path A 删除时丢失的守护（原
    test_process_stream_applies_pending_reload_before_skill_turbo_resume）：
    resume 重放 skill_acceleration_exec 会从中断点恢复原模型配置，会话中途
    排队的 pending reload 必须在重放前生效，否则 resume 按旧配置跑完、
    reload 被整体跳过。此处以模型配置校验失败提前收口流，仅验证 reload
    分支的触发条件（resume 请求触发 / 普通请求不触发）。
    """
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._instance = SimpleNamespace()
    adapter._deepresearch_rewrite_tx_uncertain = False
    reload_calls: list[str] = []

    async def apply_pending_reload():
        reload_calls.append("reload")

    adapter._maybe_apply_pending_reload = apply_pending_reload  # type: ignore[method-assign]
    adapter._refresh_stale_mcp_tool_lists = lambda: _async_none()  # type: ignore[method-assign]
    adapter._inject_extension_config_into_inputs = lambda *_args: None  # type: ignore[method-assign]
    adapter._is_subagent_approval_answer = lambda *_args: False  # type: ignore[method-assign]
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.agent_adapter.interface_deep._is_subagent_approval_answer",
        adapter._is_subagent_approval_answer,
    )
    adapter._has_valid_model_config = lambda _model_name: False  # type: ignore[method-assign]

    request = AgentRequest(
        request_id="req-reload-order",
        channel_id="test-channel",
        session_id="sess-reload-order",
        req_method=ReqMethod.CHAT_SEND,
        params=params,
    )
    chunks = [
        chunk async for chunk in adapter._process_message_stream_impl(request, {})
    ]

    assert (len(reload_calls) == 1) is expect_reload
    assert chunks and chunks[-1].is_complete is True
