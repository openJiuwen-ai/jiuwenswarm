# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""FlashRepeatCallGuardRail 单元测试（M1 ⑦）。

锁定：
1. 连续重复链：同 (工具, 规范化参数) 累计，命中 thresholds[0] 给温和提醒、后续阈值给详细提醒；
2. 深度键排序规范化：仅属性顺序不同的参数对象视为同一次重复；
3. 链路断裂（换工具/换参数）→ 计数归 1、清空提醒；
4. before_invoke（每条新消息）重置链 + 清空提醒；
5. include/exclude 通配：未跟踪工具既不计数也不重置；
6. 只提醒不否决：不拦截/改写任何工具调用（无 veto 语义）。
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.flash.rails.flash_repeat_call_guard_rail import (
    FlashRepeatCallGuardRail,
    _canonicalize,
    _wildcard_to_regex,
)


class _FakeAttachmentManager:
    def __init__(self) -> None:
        self.sections: dict[tuple[str, str], str] = {}

    def bind_context(self, ctx) -> "_FakeAttachmentWriter":
        return _FakeAttachmentWriter(self, ctx.session.get_session_id())

    async def add_section(self, *, session_id, section, content, kind, source,
                          priority=100, metadata=None, content_kind="text/plain",
                          expires_at=None) -> None:
        self.sections[(session_id, section)] = content

    async def clear_section(self, *, session_id, section) -> None:
        self.sections.pop((session_id, section), None)


class _FakeAttachmentWriter:
    """模拟 PromptAttachmentContextWriter：会话 ID 由 ctx.session 解析。"""

    def __init__(self, manager: _FakeAttachmentManager, session_id: str) -> None:
        self._manager = manager
        self._session_id = session_id

    async def add_section(self, section, content, kind, source, *,
                          priority=100, metadata=None, content_kind="text/plain",
                          expires_at=None) -> None:
        self._manager.sections[(self._session_id, section)] = content

    async def clear_section(self, section) -> None:
        self._manager.sections.pop((self._session_id, section), None)


class _FakeAgent:
    def __init__(self, language: str = "cn") -> None:
        self.prompt_attachment_manager = _FakeAttachmentManager()
        self.system_prompt_builder = SimpleNamespace(language=language)


def _make_rail(agent, **kwargs) -> FlashRepeatCallGuardRail:
    rail = FlashRepeatCallGuardRail(**kwargs)
    rail.init(agent)
    return rail


def _tool_call(name: str, args) -> SimpleNamespace:
    return SimpleNamespace(name=name, arguments=args)


class _FakeSession:
    def __init__(self, session_id: str = "s1") -> None:
        self._session_id = session_id

    def get_session_id(self) -> str:
        return self._session_id


def _ctx(conversation_id: str = "s1") -> SimpleNamespace:
    inputs = SimpleNamespace(tool_call=None, conversation_id=conversation_id)
    return SimpleNamespace(
        inputs=inputs, extra={}, session=_FakeSession(conversation_id),
    )


async def _run(rail: FlashRepeatCallGuardRail, ctx, calls) -> None:
    """在一个 invoke 内依次执行多次工具调用（复用同一 ctx 以保留 sid）。"""
    for name, args in calls:
        ctx.inputs.tool_call = _tool_call(name, args)
        await rail.after_tool_call(ctx)


def _reminder(agent, sid: str = "s1") -> str | None:
    return agent.prompt_attachment_manager.sections.get((sid, "repeat_call_reminder"))


# ── 规范化 ───────────────────────────────────────────────


class TestCanonicalize:
    def test_deep_key_sort_normalizes_order(self) -> None:
        a = {"x": 1, "nested": {"b": 2, "a": 1}}
        b = {"nested": {"a": 1, "b": 2}, "x": 1}
        assert _canonicalize(a) == _canonicalize(b)

    def test_different_values_differ(self) -> None:
        assert _canonicalize({"x": 1}) != _canonicalize({"x": 2})

    def test_json_string_key_order_normalized(self) -> None:
        """真实 ToolCall.arguments 是 JSON 字符串：键序不同视为同一调用。"""
        a = '{"x": 1, "nested": {"b": 2, "a": 1}}'
        b = '{"nested": {"a": 1, "b": 2}, "x": 1}'
        assert _canonicalize(a) == _canonicalize(b)

    def test_json_string_whitespace_normalized(self) -> None:
        assert _canonicalize('{"a": 1, "b": [1, 2]}') == _canonicalize('{"a":1,"b":[1,2]}')

    def test_invalid_json_string_kept_as_is(self) -> None:
        assert _canonicalize("not json") == _canonicalize("not json")
        assert _canonicalize("not json") != _canonicalize("other")


class TestWildcard:
    def test_star_wildcard_anchored(self) -> None:
        pat = _wildcard_to_regex("mcp_*")
        assert pat.match("mcp_read")
        assert not pat.match("mcp")  # 需要下划线后至少一个字符
        assert not pat.match("prefix_mcp_read")  # 锚定，不许前缀

    def test_literal_metacharacters(self) -> None:
        pat = _wildcard_to_regex("a.b")
        assert pat.match("a.b")
        assert not pat.match("axb")  # `.` 按字面匹配，不匹配任意字符


# ── 重复链与提醒 ────────────────────────────────────────


class TestRepeatChain:
    @pytest.mark.asyncio
    async def test_no_reminder_below_threshold(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 2)
        assert _reminder(agent) is None

    @pytest.mark.asyncio
    async def test_gentle_reminder_at_first_threshold(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 3)
        reminder = _reminder(agent)
        assert reminder is not None
        assert "重复" in reminder and "read_file" not in reminder  # 温和提醒不点名工具

    @pytest.mark.asyncio
    async def test_detailed_reminder_at_later_threshold(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 5)
        reminder = _reminder(agent)
        assert reminder is not None
        assert "read_file" in reminder and "5" in reminder

    @pytest.mark.asyncio
    async def test_chain_breaks_on_different_args(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 3)
        assert _reminder(agent) is not None
        # 换参数 → 链路断裂 → 计数归 1、提醒清空
        await _run(rail, ctx, [("read_file", {"path": "b.txt"})])
        assert _reminder(agent) is None

    @pytest.mark.asyncio
    async def test_chain_breaks_on_different_tool(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 3)
        assert _reminder(agent) is not None
        await _run(rail, ctx, [("grep", {"pattern": "x"})])
        assert _reminder(agent) is None

    @pytest.mark.asyncio
    async def test_key_order_is_same_repeat(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("write_file", {"path": "a", "content": "x"})] * 2)
        # 第三次参数键序不同，仍算同一次重复（深度键排序归一）
        ctx.inputs.tool_call = _tool_call("write_file", {"content": "x", "path": "a"})
        await rail.after_tool_call(ctx)
        reminder = _reminder(agent)
        assert reminder is not None  # 命中 threshold[0]=3

    @pytest.mark.asyncio
    async def test_real_json_string_arguments_accumulate(self) -> None:
        """真实 ToolCall.arguments 是 JSON 字符串：键序/空白不同的等价调用应累计。"""
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)

        for raw in (
            '{"path": "a.txt", "encoding": "utf-8"}',
            '{"encoding":"utf-8","path":"a.txt"}',
            '{"path":"a.txt", "encoding": "utf-8"}',
        ):
            ctx.inputs.tool_call = _tool_call("read_file", raw)
            await rail.after_tool_call(ctx)
        assert _reminder(agent) is not None

    @pytest.mark.asyncio
    async def test_before_invoke_resets_chain(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)
        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 2)

        # 新消息（再次 before_invoke）→ 重置；之前的 2 次不算
        await rail.before_invoke(ctx)
        await _run(rail, ctx, [("read_file", {"path": "a.txt"})])
        assert _reminder(agent) is None

    @pytest.mark.asyncio
    async def test_inner_ctx_tool_calls_share_outer_session_chain(self) -> None:
        """before_invoke 挂在外层 DeepAgent、after_tool_call 桥接到内层 ReActAgent：
        两者是不同 ctx 对象、extra 不共享，会话必须按 session id 对齐到真实会话。"""
        agent = _FakeAgent()
        rail = _make_rail(agent)
        outer = _ctx("s1")
        await rail.before_invoke(outer)

        inner = _ctx("s1")  # 内层 ctx：不同对象、extra 为空
        await _run(rail, inner, [("read_file", {"path": "a.txt"})] * 3)

        assert _reminder(agent, "s1") is not None
        assert (
            "default", "repeat_call_reminder"
        ) not in agent.prompt_attachment_manager.sections


class TestTracking:
    @pytest.mark.asyncio
    async def test_excluded_tool_not_tracked(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent, exclude=("mcp_*",))
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("mcp_read", {"x": 1})] * 3)
        assert _reminder(agent) is None

    @pytest.mark.asyncio
    async def test_include_limits_tracking(self) -> None:
        agent = _FakeAgent()
        rail = _make_rail(agent, include=("read_*",))
        ctx = _ctx()
        await rail.before_invoke(ctx)

        await _run(rail, ctx, [("grep", {"pattern": "x"})] * 3)
        assert _reminder(agent) is None
        await _run(rail, ctx, [("read_file", {"path": "a"})] * 3)
        assert _reminder(agent) is not None


class TestNoVeto:
    @pytest.mark.asyncio
    async def test_guard_never_vetoes(self) -> None:
        """只提醒不否决：after_tool_call 不抛异常、不改写 ctx、不拦截调用。"""
        agent = _FakeAgent()
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)
        # 大量重复也不产生 veto 语义——只是持续 upsert 提醒。
        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 20)
        assert _reminder(agent) is not None

    @pytest.mark.asyncio
    async def test_missing_attachment_manager_is_safe(self) -> None:
        agent = _FakeAgent()
        agent.prompt_attachment_manager = None  # 无 manager 也不抛
        rail = _make_rail(agent)
        ctx = _ctx()
        await rail.before_invoke(ctx)
        await _run(rail, ctx, [("read_file", {"path": "a.txt"})] * 5)
