from __future__ import annotations

from types import SimpleNamespace

import pytest

from openjiuwen.core.single_agent.interrupt.state import INTERRUPTION_KEY

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


class _ToolCall:
    def __init__(self, call_id: str, name: str) -> None:
        self.id = call_id
        self.name = name


class _InterruptEntry:
    def __init__(self, tool_call: _ToolCall) -> None:
        self.tool_call = tool_call


class _AssistantMessage:
    def __init__(self, tool_calls: list[_ToolCall]) -> None:
        self.tool_calls = tool_calls


class _InterruptState:
    def __init__(self, tool_calls: list[_ToolCall]) -> None:
        self.interrupted_tools = {tc.id: _InterruptEntry(tc) for tc in tool_calls}
        self.ai_message = _AssistantMessage(tool_calls)


class _Context:
    def __init__(self, messages: list) -> None:
        self.messages = list(messages)
        self.popped: list[int] = []

    def get_messages(self) -> list:
        return list(self.messages)

    def pop_messages(self, count: int, *, with_history: bool = False) -> None:
        self.popped.append(count)
        del self.messages[-count:]


class _ContextEngine:
    def __init__(self, context: _Context) -> None:
        self.context = context
        self.saved: list = []

    def get_context(self, session_id=None):
        return self.context

    async def save_contexts(self, session) -> None:
        self.saved.append(session)


class _LoopSession:
    def __init__(self, session_id: str, state=None) -> None:
        self.session_id = session_id
        self.states = {INTERRUPTION_KEY: state} if state is not None else {}
        self.updates: list[dict] = []
        self.commits = 0

    def get_session_id(self) -> str:
        return self.session_id

    def get_state(self, key):
        return self.states.get(key)

    def update_state(self, patch: dict) -> None:
        self.updates.append(dict(patch))
        self.states.update(patch)

    async def commit(self) -> None:
        self.commits += 1


class _FakeTask:
    def __init__(self, *, done: bool) -> None:
        self._done = done

    def done(self) -> bool:
        return self._done


def _make_adapter(
    loop_session: _LoopSession | None,
    *,
    invoke_active: bool = False,
    stream_task: _FakeTask | None = None,
    active_counts: dict | None = None,
    context_engine: _ContextEngine | None = None,
) -> JiuWenSwarmDeepAdapter:
    """object.__new__ 绕过重型 __init__，只挂清理路径真正读取的属性。"""
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    instance = SimpleNamespace(
        _loop_session=loop_session,
        _invoke_active=invoke_active,
        _stream_process_task=stream_task,
    )
    if context_engine is not None:
        instance.react_agent = SimpleNamespace(context_engine=context_engine)
    adapter._instance = instance
    adapter._active_session_ids = dict(active_counts or {})
    # 缺省视为开关打开，保证用例与环境配置解耦；开关自身的读取逻辑单测覆盖。
    adapter._related_mismatch_enabled = lambda: True
    return adapter


def _ask_user_state() -> _InterruptState:
    return _InterruptState([_ToolCall("call-1", "ask_user")])


@pytest.mark.asyncio
async def test_related_mismatch_live_session_refuses() -> None:
    """隐患 1 回归：活跃子会话 A_B 的中断态绝不能被父会话 A 的新轮清掉。

    判活走真实实现（invoke_active + 未完成的 stream task）。
    """
    loop = _LoopSession("A_B", _ask_user_state())
    adapter = _make_adapter(
        loop,
        invoke_active=True,
        stream_task=_FakeTask(done=False),
    )
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=False, force=True
    )
    assert cleared is False
    assert loop.updates == []
    assert loop.commits == 0
    assert loop.states[INTERRUPTION_KEY] is not None


@pytest.mark.asyncio
async def test_related_mismatch_live_via_active_count_refuses() -> None:
    """判活的另一条真实路径：活跃计数（非 stream 任务）同样拦截。"""
    loop = _LoopSession("A_B", _ask_user_state())
    adapter = _make_adapter(loop, active_counts={"A_B": 1})
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=False, force=True
    )
    assert cleared is False
    assert loop.updates == []
    assert loop.commits == 0


@pytest.mark.asyncio
async def test_related_mismatch_idle_session_clears_state_only() -> None:
    """核心修复：换绑后的陈旧残留（子会话已结束）在 fresh turn 下被清掉。

    且 mismatch 路径只清 state——不 pop 任何一侧 context、不做跨会话
    save_contexts（爆炸半径收敛）。
    """
    loop = _LoopSession("A_B", _ask_user_state())
    engine = _ContextEngine(_Context([object()]))
    adapter = _make_adapter(loop, context_engine=engine)
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=False, force=True
    )
    assert cleared is True
    assert loop.states[INTERRUPTION_KEY] is None
    assert loop.commits == 1
    assert engine.saved == []
    assert engine.context.popped == []


@pytest.mark.asyncio
async def test_unrelated_mismatch_still_refuses() -> None:
    """保守边界：无关会话（兄弟/其他根）不放宽，行为与修复前一致。"""
    loop = _LoopSession("B", _ask_user_state())
    adapter = _make_adapter(loop)
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=False, force=True
    )
    assert cleared is False
    assert loop.updates == []
    assert loop.commits == 0


@pytest.mark.asyncio
async def test_default_loop_session_still_refuses() -> None:
    """loop 绑在 ``default`` 时 loop_sid 归一为 None —— 保守版不放宽该形态。"""
    loop = _LoopSession("default", _ask_user_state())
    adapter = _make_adapter(loop)
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=False, force=True
    )
    assert cleared is False
    assert loop.updates == []


@pytest.mark.asyncio
async def test_reverse_direction_idle_clears() -> None:
    """反向场景（loop=父 A、target=子 A_B）：空闲时同样允许清理。

    `_is_related_session` 双向判定，锁定两个方向语义一致。
    """
    loop = _LoopSession("A", _ask_user_state())
    engine = _ContextEngine(_Context([object()]))
    adapter = _make_adapter(loop, context_engine=engine)
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A_B", require_pure_ask_user=False, force=True
    )
    assert cleared is True
    assert loop.states[INTERRUPTION_KEY] is None
    assert loop.commits == 1
    assert engine.saved == []


@pytest.mark.asyncio
async def test_reverse_direction_live_refuses() -> None:
    """反向场景（loop=父 A、target=子 A_B）：父会话活跃（子代理在跑）时拒绝。"""
    loop = _LoopSession("A", _ask_user_state())
    adapter = _make_adapter(
        loop,
        invoke_active=True,
        stream_task=_FakeTask(done=False),
    )
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A_B", require_pure_ask_user=False, force=True
    )
    assert cleared is False
    assert loop.updates == []
    assert loop.commits == 0


@pytest.mark.asyncio
async def test_kill_switch_restores_old_behaviour(monkeypatch) -> None:
    """开关关闭：related mismatch 回退为拒绝（旧行为），供应急回退。"""
    loop = _LoopSession("A_B", _ask_user_state())
    adapter = _make_adapter(loop)
    monkeypatch.setattr(adapter, "_related_mismatch_enabled", lambda: False)
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=False, force=True
    )
    assert cleared is False
    assert loop.updates == []
    assert loop.commits == 0


@pytest.mark.asyncio
async def test_exact_match_clears_with_context_tail() -> None:
    """exact-match 原路径回归：签名匹配时仍走 pop + save_contexts + commit。"""
    tc = _ToolCall("call-1", "ask_user")
    loop = _LoopSession("A", _InterruptState([tc]))
    context = _Context([_AssistantMessage([tc])])
    engine = _ContextEngine(context)
    adapter = _make_adapter(loop, context_engine=engine)
    cleared = await adapter._clear_pending_interaction_interrupt(
        "A", require_pure_ask_user=True, force=False
    )
    assert cleared is True
    assert loop.states[INTERRUPTION_KEY] is None
    assert context.popped == [1]
    assert engine.saved == [loop]
    assert loop.commits == 1


@pytest.mark.asyncio
async def test_non_force_non_pure_ask_user_still_rejected() -> None:
    """cancel/supplement（非 force）路径行为不变：非纯 ask_user 一律拒绝。"""
    loop = _LoopSession("A", _InterruptState([_ToolCall("call-1", "bash")]))
    adapter = _make_adapter(loop)
    cleared = await adapter._clear_pending_interaction_interrupt("A")
    assert cleared is False
    assert loop.updates == []
    assert loop.commits == 0


def test_related_mismatch_enabled_reads_config(monkeypatch) -> None:
    """开关读取逻辑：配置为 false 时返回 False；缺省/异常时回退 True。"""
    import jiuwenswarm.common.config as config_mod

    class _FakeConfig:
        def __init__(self, data: dict) -> None:
            self._data = data

        def get(self, key, default=None):
            return self._data.get(key, default)

        def __bool__(self) -> bool:
            return bool(self._data)

    monkeypatch.setattr(
        config_mod,
        "get_config",
        lambda: _FakeConfig(
            {
                "execution_guard": {
                    "stale_interrupt_clear": {"related_mismatch_enabled": False}
                }
            }
        ),
    )
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    assert adapter._related_mismatch_enabled() is False

    monkeypatch.setattr(config_mod, "get_config", lambda: None)
    assert adapter._related_mismatch_enabled() is True
