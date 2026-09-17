# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for jiuwenswarm.server.hooks.user_hook_rail."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any
from unittest.mock import AsyncMock
import pytest

from jiuwenswarm.common.hooks_config import HooksConfig, HookMatcher
from jiuwenswarm.server.hooks.user_hook_rail import UserHookRail


# ============================================================
# Mock helpers: 模拟 openjiuwen 的 AgentCallbackContext
# ============================================================

@dataclass
class MockToolInputs:
    tool_name: str = ""
    tool_args: Any = None
    tool_result: Any = None
    tool_msg: Any = None


@dataclass
class MockCallbackContext:
    inputs: MockToolInputs = field(default_factory=MockToolInputs)
    extra: dict = field(default_factory=dict)
    session: Any = None


# ============================================================
# UserHookRail: before_tool_call (PreToolUse)
# ============================================================

class TestBeforeToolCall:
    @staticmethod
    def _make_config(**events):
        matchers = {}
        for event_name, matcher_list in events.items():
            matchers[event_name] = [
                HookMatcher(matcher=m[0], hooks=m[1]) for m in matcher_list
            ]
        return HooksConfig(events=matchers)

    @pytest.mark.asyncio
    async def test_no_matching_hooks_does_nothing(self):
        config = self._make_config()
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Write"))
        await rail.before_tool_call(ctx)
        # ctx 不应被修改
        assert "_skip_tool" not in ctx.extra

    @pytest.mark.asyncio
    async def test_blocking_hook_sets_skip_tool(self):
        config = self._make_config(
            PreToolUse=[("*", [{"command": "echo block >&2; exit 2", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.before_tool_call(ctx)
        assert ctx.extra["_skip_tool"] is True
        assert "_hook_feedback" in ctx.extra

    @pytest.mark.asyncio
    async def test_modifying_hook_updates_tool_args(self):
        config = self._make_config(
            PreToolUse=[(
                "Write",
                [{
                    "command": 'echo \'{"modifiedInput":{"file_path":"/safe/path.txt"}}\'',
                    "timeout": 5,
                }]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(
            inputs=MockToolInputs(
                tool_name="Write",
                tool_args={"file_path": "/dangerous/path.txt"},
            )
        )
        await rail.before_tool_call(ctx)
        assert ctx.inputs.tool_args == {"file_path": "/safe/path.txt"}

    @pytest.mark.asyncio
    async def test_modifying_hook_updates_tool_name(self):
        """modifiedInput 中的 _tool_name 可改变工具名."""
        config = self._make_config(
            PreToolUse=[(
                "*",
                [{
                    "command": 'echo \'{"modifiedInput":{"_tool_name":"Read","file_path":"/tmp/x.txt"}}\'',
                    "timeout": 5,
                }]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Write"))
        await rail.before_tool_call(ctx)
        assert ctx.inputs.tool_name == "Read"

    @pytest.mark.asyncio
    async def test_matcher_filters_by_tool_name(self):
        """只有匹配的工具名才会触发 hook."""
        config = self._make_config(
            PreToolUse=[("Write", [{"command": "exit 2", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        # Bash 不匹配 Write → 不会被阻止
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.before_tool_call(ctx)
        assert "_skip_tool" not in ctx.extra

    @pytest.mark.asyncio
    async def test_additional_context_appended(self):
        config = self._make_config(
            PreToolUse=[(
                "*",
                [{"command": 'echo \'{"additionalContext":"pre-write check passed"}\'', "timeout": 5}]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Write"))
        await rail.before_tool_call(ctx)
        assert "pre-write check passed" in ctx.extra.get("_hook_additional_context", "")

    @pytest.mark.asyncio
    async def test_blocking_takes_priority_over_modify(self):
        """第一个 blocking hook 应阻止，后续结果不再处理."""
        config = self._make_config(
            PreToolUse=[(
                "Bash",
                [
                    {"command": "echo block >&2; exit 2", "timeout": 5},
                    {"command": 'echo \'{"modifiedInput":{"safe":"yes"}}\'', "timeout": 5},
                ]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.before_tool_call(ctx)
        assert ctx.extra["_skip_tool"] is True
        # modifiedInput 不应该被应用（因为早已 return）
        assert "safe" not in str(ctx.inputs.tool_args)

    @pytest.mark.asyncio
    async def test_empty_tool_name_handled(self):
        """空工具名不应崩溃."""
        config = self._make_config(
            PreToolUse=[("*", [{"command": "echo ok", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name=""))
        await rail.before_tool_call(ctx)
        # 不应抛出异常

    @pytest.mark.asyncio
    async def test_no_matching_matchers_skip_execution(self):
        """没有匹配的 matcher 时，不应执行任何 hook."""
        config = self._make_config(
            PreToolUse=[("Write", [{"command": "echo ok", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.before_tool_call(ctx)
        assert "_skip_tool" not in ctx.extra

    @pytest.mark.asyncio
    async def test_passes_session_id_from_context_session(self):
        config = self._make_config(
            PreToolUse=[("*", [{"command": "echo ok", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        rail._executor.run_all = AsyncMock(return_value=[])
        ctx = MockCallbackContext(
            inputs=MockToolInputs(tool_name="Bash"),
            session=type("Session", (), {"get_session_id": lambda self: "session-2747"})(),
        )

        await rail.before_tool_call(ctx)

        hook_input = rail._executor.run_all.await_args.kwargs["hook_input"]
        assert hook_input["session_id"] == "session-2747"


# ============================================================
# UserHookRail: after_tool_call (PostToolUse)
# ============================================================

class TestAfterToolCall:
    @staticmethod
    def _make_config(**events):
        matchers = {}
        for event_name, matcher_list in events.items():
            matchers[event_name] = [
                HookMatcher(matcher=m[0], hooks=m[1]) for m in matcher_list
            ]
        return HooksConfig(events=matchers)

    @pytest.mark.asyncio
    async def test_no_matching_hooks_does_nothing(self):
        config = self._make_config()
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Write"))
        await rail.after_tool_call(ctx)
        assert "_post_tool_hook_feedback" not in ctx.extra

    @pytest.mark.asyncio
    async def test_appends_additional_context_to_result(self):
        config = self._make_config(
            PostToolUse=[(
                "*",
                [{"command": 'echo \'{"additionalContext":"review note"}\'', "timeout": 5}]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(
            inputs=MockToolInputs(tool_name="Bash", tool_result="command output")
        )
        await rail.after_tool_call(ctx)
        assert "review note" in ctx.inputs.tool_result
        assert "command output" in ctx.inputs.tool_result  # 原内容保留

    @pytest.mark.asyncio
    async def test_additional_context_appended_to_none_result(self):
        """即使 tool_result 为 None，也能正常工作."""
        config = self._make_config(
            PostToolUse=[(
                "*",
                [{"command": 'echo \'{"additionalContext":"note"}\'', "timeout": 5}]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Read", tool_result=None))
        await rail.after_tool_call(ctx)
        assert "note" in (ctx.inputs.tool_result or "")

    @pytest.mark.asyncio
    async def test_blocking_post_tool_triggers_feedback(self):
        config = self._make_config(
            PostToolUse=[(
                "Bash",
                [{"command": "echo 'blocked after review' >&2; exit 2", "timeout": 5}]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.after_tool_call(ctx)
        assert "_post_tool_hook_feedback" in ctx.extra


# ============================================================
# UserHookRail: on_tool_exception (PostToolUseFailure)
# ============================================================

class TestOnToolException:
    @staticmethod
    def _make_config(**events):
        matchers = {}
        for event_name, matcher_list in events.items():
            matchers[event_name] = [
                HookMatcher(matcher=m[0], hooks=m[1]) for m in matcher_list
            ]
        return HooksConfig(events=matchers)

    @pytest.mark.asyncio
    async def test_no_matching_hooks_does_nothing(self):
        config = self._make_config()
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Write"))
        await rail.on_tool_exception(ctx)
        # 不应崩溃

    @pytest.mark.asyncio
    async def test_hook_runs_but_does_not_block(self):
        """PostToolUseFailure 只收集信息，不改变异常处理流程."""
        config = self._make_config(
            PostToolUseFailure=[(
                "*",
                [{"command": "echo 'error logged'", "timeout": 5}]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.on_tool_exception(ctx)
        # 不应修改 ctx.extra（结果被丢弃）


# ============================================================
# UserHookRail: after_invoke (Stop)
# ============================================================

class TestAfterInvoke:
    @staticmethod
    def _make_config(**events):
        matchers = {}
        for event_name, matcher_list in events.items():
            matchers[event_name] = [
                HookMatcher(matcher=m[0], hooks=m[1]) for m in matcher_list
            ]
        return HooksConfig(events=matchers)

    @pytest.mark.asyncio
    async def test_no_matching_hooks_does_nothing(self):
        config = self._make_config()
        rail = UserHookRail(config)
        ctx = MockCallbackContext()
        await rail.after_invoke(ctx)
        assert "_stop_hook_feedback" not in ctx.extra

    @pytest.mark.asyncio
    async def test_blocking_stop_hook_sets_feedback(self):
        config = self._make_config(
            Stop=[("*", [{"command": "echo 'final check failed' >&2; exit 2", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext()
        await rail.after_invoke(ctx)
        assert "_stop_hook_feedback" in ctx.extra

    @pytest.mark.asyncio
    async def test_stop_matches_without_tool_name(self):
        """Stop 事件不按 tool_name 过滤，match 时 query 为空."""
        config = self._make_config(
            Stop=[("*", [{"command": "echo stop", "timeout": 5}])]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext()
        await rail.after_invoke(ctx)
        # 不应崩溃

    @pytest.mark.asyncio
    async def test_multiple_stop_hooks_both_run(self):
        config = self._make_config(
            Stop=[(
                "*",
                [
                    {"command": "echo first", "timeout": 5},
                    {"command": "echo second; exit 2", "timeout": 5},
                ]
            )]
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext()
        await rail.after_invoke(ctx)
        # 即使第二个 hook 是 blocking，也应记录 feedback
        assert "_stop_hook_feedback" in ctx.extra


# ============================================================
# UserHookRail: priority
# ============================================================

class TestPriority:
    @staticmethod
    def test_priority_is_60():
        config = HooksConfig()
        rail = UserHookRail(config)
        assert rail.priority == 60


# ============================================================
# UserHookRail: disable_all_hooks
# ============================================================

class TestDisableAllHooks:
    @pytest.mark.asyncio
    async def test_disabled_hooks_skip_all(self):
        config = HooksConfig(
            events={
                "PreToolUse": [HookMatcher(matcher="*", hooks=[{"command": "exit 2", "timeout": 5}])],
                "Stop": [HookMatcher(matcher="*", hooks=[{"command": "exit 2", "timeout": 5}])],
            },
            disable_all_hooks=True,
        )
        rail = UserHookRail(config)
        ctx = MockCallbackContext(inputs=MockToolInputs(tool_name="Bash"))
        await rail.before_tool_call(ctx)
        assert "_skip_tool" not in ctx.extra
