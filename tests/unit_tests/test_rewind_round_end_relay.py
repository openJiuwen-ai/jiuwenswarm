"""回归测试：轮末上下文持久化与 rewind 后 session_ref 复位。

覆盖范围：
    - ``DeepAgent.run_one_round`` 在 ``session.commit()`` 之前
      显式调用 ``context_engine.save_contexts(session)``，且传入的就是
      即将 commit 的同一个交互 session。这样无论 react_agent 走到哪个
      终结分支（含非 AssistantMessage break、resume 短路），内存中新增
      的消息都能在轮末持久化到 state，且不受 session_ref 被改写影响。
    - ``JiuWenSwarmDeepAdapter._write_rewind_context`` 在
      rewind 收尾把活窗 context 的 ``session_ref`` 复位回交互 session
      （``_orig_session``），避免后续不显式传 session 的写者（如压缩
      链路）写到已被抛弃的 ``_presess`` 上。
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from openjiuwen.harness import deep_agent as deep_agent_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

logger = logging.getLogger(__name__)


def _snapshot(tag: str, live_ctx, orig_session, presess) -> None:
    """打印 rewind 前后 live_ctx.session_ref 与相关 message 数的对比快照。"""
    ref = getattr(live_ctx, "session_ref", None)
    ref_name = getattr(ref, "name", None) if ref is not None else None
    msgs = list(getattr(live_ctx, "messages", []) or [])
    roles = [type(m).__name__ for m in msgs]
    logger.info(
        "[%s] live_ctx.session_ref=%s (orig=%s, presess=%s) | msgs=%s roles=%s",
        tag,
        ref_name,
        getattr(orig_session, "name", None),
        getattr(presess, "name", None),
        str(msgs),
        roles,
    )


# ---------------------------------------------------------------------------
# 轮末 commit 前无条件保存上下文
# ---------------------------------------------------------------------------


def _find_run_one_round() -> ast.AsyncFunctionDef:
    """定位 DeepAgent.run_one_round 的 AST 节点。"""
    src = inspect.getsource(deep_agent_module)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef) and node.name == "DeepAgent":
            for child in node.body:
                if isinstance(child, ast.AsyncFunctionDef) and child.name == "run_one_round":
                    return child
    raise AssertionError("DeepAgent.run_one_round not found")


def _calls_in_order(fn: ast.AsyncFunctionDef) -> list[str]:
    """按源码顺序抽出所有 await 调用的可读名称（attr 链）。"""
    names: list[str] = []

    def _render(node: ast.AST) -> str:
        if isinstance(node, ast.Attribute):
            return f"{_render(node.value)}.{node.attr}"
        if isinstance(node, ast.Name):
            return node.id
        if isinstance(node, ast.Call):
            return _render(node.func)
        return ""

    def _visit(node: ast.AST) -> None:
        for child in ast.iter_child_nodes(node):
            if isinstance(child, ast.Await) and isinstance(child.value, ast.Call):
                names.append(_render(child.value.func))
            _visit(child)

    _visit(fn)
    return names


def test_run_one_round_saves_contexts_before_commit():
    """轮末 commit 之前必须先 save_contexts(session)。"""
    fn = _find_run_one_round()
    calls = _calls_in_order(fn)

    # 找到 commit 与 save_contexts 的相对位置
    save_idx = next(
        (i for i, name in enumerate(calls) if name.endswith("context_engine.save_contexts")),
        None,
    )
    commit_idx = next(
        (i for i, name in enumerate(calls) if name == "session.commit"),
        None,
    )

    assert save_idx is not None, (
        "run_one_round 缺少 await self.context_engine.save_contexts(...) 调用"
    )
    assert commit_idx is not None, "run_one_round 缺少 await session.commit() 调用"
    assert save_idx < commit_idx, (
        f"save_contexts 必须在 commit 之前调用，实际顺序：{calls}"
    )


def test_run_one_round_save_contexts_uses_commit_session():
    """save_contexts 的实参必须是即将 commit 的同一个 session 变量。"""
    fn = _find_run_one_round()

    save_call: ast.Call | None = None
    commit_call: ast.Call | None = None
    for node in ast.walk(fn):
        if isinstance(node, ast.Await) and isinstance(node.value, ast.Call):
            call = node.value
            func = call.func
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "save_contexts"
                and isinstance(func.value, ast.Attribute)
                and func.value.attr == "context_engine"
            ):
                save_call = call
            if (
                isinstance(func, ast.Attribute)
                and func.attr == "commit"
                and isinstance(func.value, ast.Name)
                and func.value.id == "session"
            ):
                commit_call = call

    if save_call is None:
        # ReAct 已有轮末保存；压缩 rail 负责在交互 session 上中继同一保存动作。
        from openjiuwen.harness.rails.context_engineer.context_processor_rail import (
            ContextProcessorRail,
        )

        rail_source = inspect.getsource(ContextProcessorRail.after_react_iteration)
        rail_tree = ast.parse(textwrap.dedent(rail_source))
        for node in ast.walk(rail_tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
                if node.func.attr == "save_contexts":
                    save_call = node
                if node.func.attr == "commit":
                    commit_call = node
        assert commit_call is not None
        assert isinstance(commit_call.func.value, ast.Attribute)
        assert commit_call.func.value.attr == "session"
        assert isinstance(save_call.args[0], ast.Attribute)
        assert save_call.args[0].attr == "session"
        return

    assert commit_call is not None
    assert save_call.args, "save_contexts 必须显式传入 session"
    first_arg = save_call.args[0]
    assert isinstance(first_arg, ast.Name) and first_arg.id == "session", (
        "save_contexts 必须传入名为 `session` 的局部变量（即 run_one_round 的形参 session，"
        "也就是 commit 所使用的同一个交互 session），不能用 get_session_ref() 等替代"
    )


def test_run_one_round_save_contexts_failure_does_not_block_commit():
    """save_contexts 失败只告警，不阻断后续 commit。"""
    fn = _find_run_one_round()
    # 找到 save_contexts 所在的最内层 try 块，确认 commit 不在其 body 内
    for node in ast.walk(fn):
        if not isinstance(node, ast.Try):
            continue
        # 仅看 try 体的**直接子语句**，不递归进入嵌套 try
        direct_body_src = ast.dump(ast.Module(body=node.body, type_ignores=[]))
        has_save = "save_contexts" in direct_body_src
        if not has_save:
            continue
        # save_contexts 直接出现在该 try 体里；同层不应再有 commit
        has_commit_at_same_level = any(
            isinstance(child, ast.Await)
            and isinstance(child.value, ast.Call)
            and isinstance(child.value.func, ast.Attribute)
            and child.value.func.attr == "commit"
            for stmt in node.body
            for child in ast.walk(stmt)
            if not isinstance(stmt, ast.Try)
        )
        assert not has_commit_at_same_level, (
            "save_contexts 与 commit 不应在同一个 try 块内，"
            "否则 save_contexts 抛错会跳过 commit"
        )
        assert node.handlers, "save_contexts 必须有 except 兜底"
        return
    raise AssertionError("未找到 save_contexts 所在的 try 块")


# ---------------------------------------------------------------------------
# rewind 收尾复位活窗 context 的 session_ref
# ---------------------------------------------------------------------------


def _make_adapter_for_rewind():
    """构造一个最小可用的 JiuWenSwarmDeepAdapter 实例用于调用 _write_rewind_context。"""
    # 用真实字段而非 MagicMock，便于在日志里观察 session_ref 的前后变化
    live_ctx = SimpleNamespace(
        context_id=MagicMock(return_value="default_context_id"),
        load_state=MagicMock(),
        session_ref=None,           # rewind 期间会被 set_session_ref 改写
        messages=[],                # 活窗池当前的消息列表
    )
    # 用真实方法替代 MagicMock，让 session_ref 真正被赋值
    def _set_session_ref(sess):
        live_ctx.session_ref = sess
    live_ctx.set_session_ref = MagicMock(side_effect=_set_session_ref)

    rewind_ctx = SimpleNamespace(
        context_id=MagicMock(return_value="default_context_id"),
        load_state=MagicMock(),
    )
    ctx_eng = SimpleNamespace(
        create_context=AsyncMock(return_value=rewind_ctx),
        save_contexts=AsyncMock(),
        get_context=MagicMock(return_value=live_ctx),
    )

    orig_session = SimpleNamespace(name="orig_session")
    presess = SimpleNamespace(
        name="presess",
        update_state=MagicMock(),
        _pre_run_done=False,
    )

    instance = SimpleNamespace(
        react_agent=SimpleNamespace(context_engine=ctx_eng),
        _interaction_session=orig_session,
        _loop_session=None,
        save_state=MagicMock(),
    )

    adapter = JiuWenSwarmDeepAdapter.__new__(JiuWenSwarmDeepAdapter)
    adapter._instance = instance
    adapter._rewind_session = {}

    return adapter, live_ctx, rewind_ctx, ctx_eng, orig_session, presess


@pytest.mark.asyncio
async def test_write_rewind_context_restores_live_session_ref(caplog):
    """rewind 收尾把活窗 context 的 session_ref 复位回 _orig_session。"""
    caplog.set_level(logging.INFO, logger=__name__)
    adapter, live_ctx, _rewind_ctx, ctx_eng, orig_session, presess = _make_adapter_for_rewind()

    # 模拟 rewind 前状态：活窗已有交付消息，session_ref 指向交互 session
    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        ToolMessage,
        UserMessage,
    )
    live_ctx.messages = [
        UserMessage(content="查询今天最热门的体育新闻生成word发我"),
        AssistantMessage(content="", tool_calls=[]),
        ToolMessage(tool_call_id="tc1", content="Sent 1 files"),
        AssistantMessage(content="Word 文档已经生成并发送给你了"),
    ]
    live_ctx.session_ref = orig_session
    _snapshot("BEFORE rewind", live_ctx, orig_session, presess)

    # 模拟 rewind 期间 session_ref 被改写：create_context(session=_presess)
    async def _override_create_context(*_args, **kwargs):
        live_ctx.session_ref = kwargs["session"]  # set_session_ref(_presess)
        # rewind 同时用旧快照覆盖消息列表
        live_ctx.messages = [
            UserMessage(content="查询今天最热门的体育新闻生成word发我"),
            AssistantMessage(content="", tool_calls=[]),
            ToolMessage(tool_call_id="tc1", content="ModuleNotFoundError: python-docx"),
            ToolMessage(tool_call_id="tc2", content="[181001] ... 408 Request Timeout"),
        ]
        _snapshot("DURING rewind (ref overwritten)", live_ctx, orig_session, presess)
        return _rewind_ctx
    ctx_eng.create_context = AsyncMock(side_effect=_override_create_context)

    await adapter._write_rewind_context(
        session_id="sess-1",
        reason="stream_error",
        messages=[MagicMock(), MagicMock()],
        _presess=presess,
    )
    _snapshot("AFTER rewind", live_ctx, orig_session, presess)

    # create_context 用 _presess 调用（rewind 重建）
    ctx_eng.create_context.assert_awaited()
    create_kwargs = ctx_eng.create_context.await_args.kwargs
    assert create_kwargs["session"] is presess

    # save_contexts 至少调用了 _presess 一次
    presess_saved = any(
        call.args and call.args[0] is presess for call in ctx_eng.save_contexts.await_args_list
    )
    assert presess_saved, "save_contexts 必须先把 rewind 结果写入 _presess"

    # 关键断言：rewind 末尾必须把活窗 context 的 session_ref 复位回 _orig_session
    live_ctx.set_session_ref.assert_called_once_with(orig_session)
    assert live_ctx.session_ref is orig_session, (
        "rewind 末尾必须把 session_ref 复位回 orig_session，"
        "否则后续压缩链路会写到已抛弃的 _presess 上"
    )


@pytest.mark.asyncio
async def test_write_rewind_context_skips_restore_when_orig_is_presess():
    """_orig_session 与 _presess 相同（未被改写）时不调用 set_session_ref。"""
    adapter, live_ctx, _rewind_ctx, ctx_eng, _orig, presess = _make_adapter_for_rewind()
    # 让 _interaction_session 就是 _presess 自己
    adapter._instance._interaction_session = presess
    adapter._instance._loop_session = None

    await adapter._write_rewind_context(
        session_id="sess-1",
        reason="stream_error",
        messages=[MagicMock()],
        _presess=presess,
    )

    live_ctx.set_session_ref.assert_not_called()


@pytest.mark.asyncio
async def test_write_rewind_context_skips_restore_when_no_orig_session():
    """找不到 _orig_session 时静默跳过，不影响 rewind 主流程。"""
    adapter, live_ctx, _rewind_ctx, ctx_eng, _orig, presess = _make_adapter_for_rewind()
    adapter._instance._interaction_session = None
    adapter._instance._loop_session = None

    await adapter._write_rewind_context(
        session_id="sess-1",
        reason="stream_error",
        messages=[MagicMock()],
        _presess=presess,
    )

    live_ctx.set_session_ref.assert_not_called()
    # 主流程仍应走到 save_state + 预置 rewind session
    adapter._instance.save_state.assert_called_once_with(presess)
    assert adapter._rewind_session  # 预置 rewind session for next invoke


@pytest.mark.asyncio
async def test_write_rewind_context_restore_failure_is_logged(monkeypatch):
    """复位失败保留告警且不影响 rewind 主流程。"""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    warning = MagicMock()
    monkeypatch.setattr(interface_deep.logger, "warning", warning)
    adapter, live_ctx, _rewind_ctx, ctx_eng, orig_session, presess = _make_adapter_for_rewind()
    live_ctx.set_session_ref.side_effect = RuntimeError("mock restore failure")

    # 不应抛出
    await adapter._write_rewind_context(
        session_id="sess-1",
        reason="stream_error",
        messages=[MagicMock()],
        _presess=presess,
    )

    # 主流程仍然完成，且故障可通过日志定位
    adapter._instance.save_state.assert_called_once_with(presess)
    assert adapter._rewind_session
    assert "failed to restore live context session_ref" in warning.call_args.args[0]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("flush_succeeded", "history_records"),
    [(False, None), (True, [])],
    ids=["flush_early_return", "empty_history_early_return"],
)
async def test_rewind_fallback_early_return_restores_session_ref(
    monkeypatch,
    flush_succeeded,
    history_records,
):
    """降级链路不写上下文时，也不能把活窗留在临时 session 上。"""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter, live_ctx, _rewind_ctx, _ctx_eng, orig_session, presess = _make_adapter_for_rewind()
    live_ctx.session_ref = presess
    adapter._build_rewind_messages_for_model = MagicMock(return_value=[])
    monkeypatch.setattr(
        interface_deep,
        "flush_history_writes_async",
        AsyncMock(return_value=flush_succeeded),
    )
    monkeypatch.setattr(
        interface_deep,
        "load_history_records",
        MagicMock(return_value=history_records),
    )

    await adapter._rewind_history_fallback("sess-1", "stream_error", presess)

    assert live_ctx.session_ref is orig_session


@pytest.mark.asyncio
async def test_prepare_rewind_restores_session_ref_on_flush_early_return(monkeypatch):
    """基线 create_context 改写引用后，flush 早退也由 finally 复位。"""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep

    adapter, live_ctx, _rewind_ctx, ctx_eng, orig_session, _presess = _make_adapter_for_rewind()
    rewind_session = SimpleNamespace(
        pre_run=AsyncMock(),
        get_state=MagicMock(return_value="request-1"),
    )
    live_ctx.get_messages = MagicMock(return_value=[MagicMock()])
    adapter._instance.card = MagicMock()

    async def _hijack_session_ref(*_args, **kwargs):
        live_ctx.session_ref = kwargs["session"]
        return live_ctx

    ctx_eng.create_context = AsyncMock(side_effect=_hijack_session_ref)
    monkeypatch.setattr(
        "openjiuwen.core.single_agent.create_agent_session",
        MagicMock(return_value=rewind_session),
    )
    monkeypatch.setattr(
        interface_deep,
        "flush_history_writes_async",
        AsyncMock(return_value=False),
    )

    await adapter._prepare_rewind_session_for_next_invoke("sess-1", reason="stream_error")

    assert live_ctx.session_ref is orig_session


@pytest.mark.asyncio
async def test_prepare_rewind_restores_session_ref_when_fallback_fails(monkeypatch):
    """构造和全量回放均失败时，清理仍执行且不改变原异常处理语义。"""
    adapter, live_ctx, _rewind_ctx, ctx_eng, orig_session, _presess = _make_adapter_for_rewind()
    adapter._instance.card = MagicMock()
    rewind_session = SimpleNamespace(pre_run=AsyncMock())
    monkeypatch.setattr(
        "openjiuwen.core.single_agent.create_agent_session",
        MagicMock(return_value=rewind_session),
    )
    ctx_eng.create_context = AsyncMock(side_effect=RuntimeError("mock baseline failure"))

    async def _fallback_failure(*_args, **_kwargs):
        live_ctx.session_ref = rewind_session
        raise RuntimeError("mock fallback failure")

    adapter._rewind_history_fallback = AsyncMock(side_effect=_fallback_failure)
    live_ctx.session_ref = orig_session

    await adapter._prepare_rewind_session_for_next_invoke("sess-1", reason="stream_error")

    assert live_ctx.session_ref is orig_session


@pytest.mark.asyncio
async def test_write_rewind_context_skips_restore_when_no_live_context():
    """活窗 context 不存在（get_context 返回 None）时静默跳过。"""
    adapter, live_ctx, _rewind_ctx, ctx_eng, orig_session, presess = _make_adapter_for_rewind()
    ctx_eng.get_context = MagicMock(return_value=None)

    await adapter._write_rewind_context(
        session_id="sess-1",
        reason="stream_error",
        messages=[MagicMock()],
        _presess=presess,
    )

    live_ctx.set_session_ref.assert_not_called()
    adapter._instance.save_state.assert_called_once_with(presess)


# ---------------------------------------------------------------------------
# 集成式衔接：rewind 复位 session_ref + 轮末 save_contexts 持久化 ——
# rewind 后消息继续增长，轮末保存到 state，第二轮读到完整上下文（含交付事实）
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rewind_then_round_end_relay_recovers_delivery(caplog):
    """模拟 rewind 覆盖消息并改写 session_ref → 内存中继续增长
    （含交付）→ 轮末 save_contexts(session) 持久化 → 第二轮读到完整 state。

    完整链路：
      T1: 第一轮末尾内存里已有交付事实（Sent 1 files + Word 已发送）
      T2: rewind 改写 session_ref，并用旧快照覆盖消息列表
      T3: rewind 收尾把 session_ref 复位回 orig_session
      T4: 第一轮继续执行，消息重新增长到含交付事实
      T5: 轮末用同一个 orig_session 调 save_contexts → state 更新
      T6: 第二轮从 orig_session.state 读回完整上下文（含交付）
    """
    caplog.set_level(logging.INFO, logger=__name__)
    from openjiuwen.core.foundation.llm.schema.message import (
        AssistantMessage,
        ToolMessage,
        UserMessage,
    )

    adapter, live_ctx, _rewind_ctx, ctx_eng, orig_session, presess = _make_adapter_for_rewind()

    # ── 给 orig_session 一个真实的 state 字典，模拟 commit 持久化目标 ──
    orig_session.state = {}
    # 让 save_contexts 真正写入 orig_session.state（模拟 SDK 的 _save_state_to_session）
    async def _save_contexts(session, context_ids=None):
        if session is orig_session:
            # 把 live_ctx 当前的消息列表写进 state["context"]
            orig_session.state["context"] = {
                "default_context_id": {"messages": list(live_ctx.messages)}
            }
        # 写到 _presess 的调用直接吞掉（rewind 内部的临时写入）
    ctx_eng.save_contexts = AsyncMock(side_effect=_save_contexts)

    # ── T1: rewind 前状态：内存里有完整交付事实 ──
    live_ctx.messages = [
        UserMessage(content="查询今天最热门的体育新闻生成word发我"),
        AssistantMessage(content="", tool_calls=[]),
        ToolMessage(tool_call_id="tc1", content="Sent 1 files"),
        AssistantMessage(content="Word 文档已经生成并发送给你了"),
    ]
    live_ctx.session_ref = orig_session
    _snapshot("T1 BEFORE rewind (含交付)", live_ctx, orig_session, presess)

    # ── T2: rewind 改写 session_ref，并用失败快照覆盖消息列表 ──
    async def _override_create_context(*_args, **kwargs):
        live_ctx.session_ref = kwargs["session"]
        live_ctx.messages = [
            UserMessage(content="查询今天最热门的体育新闻生成word发我"),
            AssistantMessage(content="", tool_calls=[]),
            ToolMessage(tool_call_id="tc1", content="ModuleNotFoundError: python-docx"),
            ToolMessage(tool_call_id="tc2", content="[181001] ... 408 Request Timeout"),
        ]
        _snapshot("T2 DURING rewind (ref overwritten)", live_ctx, orig_session, presess)
        return _rewind_ctx
    ctx_eng.create_context = AsyncMock(side_effect=_override_create_context)

    # ── T3: 触发 rewind，收尾应复位 session_ref ──
    await adapter._write_rewind_context(
        session_id="sess-1",
        reason="stream_error",
        messages=[MagicMock()],
        _presess=presess,
    )
    _snapshot("T3 AFTER rewind (ref 已复位)", live_ctx, orig_session, presess)
    assert live_ctx.session_ref is orig_session, "rewind 收尾必须复位 session_ref"

    # ── T4: 第一轮继续执行，消息重新增长到含交付事实 ──
    # 模拟模型重做依赖安装、检索、生成、发送、最终答复
    live_ctx.messages.extend([
        ToolMessage(tool_call_id="tc9", content="Sent 1 files"),
        AssistantMessage(content="Word 文档已经生成并发送给你了，请查收今日体育热点速览。"),
    ])
    _snapshot("T4 池内重新增长 (含交付)", live_ctx, orig_session, presess)

    # ── T5: 轮末强制 save_contexts(orig_session) ──
    # 直接调用 save_contexts，模拟 deep_agent.run_one_round 在 commit 前的保存
    await ctx_eng.save_contexts(orig_session)
    _snapshot("T5 轮末 save_contexts 后", live_ctx, orig_session, presess)

    # ── T6: 第二轮从 orig_session.state 读回完整上下文 ──
    second_round_state = orig_session.state.get("context", {})
    second_round_msgs = second_round_state.get("default_context_id", {}).get("messages", [])
    logger.info(
        "[T6 第二轮读取] state msgs=%d contents=%s",
        len(second_round_msgs),
        [getattr(m, "content", "") for m in second_round_msgs],
    )

    # 核心断言：第二轮必须看到完整上下文（含交付事实），不是 rewind 定格的 4 条旧快照
    assert len(second_round_msgs) == 6, (
        f"第二轮应读到 6 条（含交付），实际 {len(second_round_msgs)} 条"
    )
    joined = " ".join(str(getattr(m, "content", "") or "") for m in second_round_msgs)
    assert "Sent 1 files" in joined, "第二轮必须看到 Sent 1 files"
    assert "Word 文档已经生成并发送给你了" in joined, "第二轮必须看到交付答复"
    # 也不应丢失 rewind 定格的失败事实（它们也是历史的一部分）
    assert "408" in joined, "第二轮仍应保留 408 失败事实"
