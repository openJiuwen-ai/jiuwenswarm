# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for jiuwenswarm.common.hooks_config."""

from __future__ import annotations

from jiuwenswarm.common.hooks_config import (
    HooksConfig,
    HookEvent,
    HookMatcher,
    CommandHookConfig,
    PromptHookConfig,
    load_hooks_config,
    is_rail_event,
    is_gateway_event,
)


class TestHookEvent:
    """测试 HookEvent 枚举."""

    @staticmethod
    def test_all_17_events_defined():
        assert len(HookEvent) == 17

    @staticmethod
    def test_tool_events():
        assert HookEvent.PRE_TOOL_USE.value == "PreToolUse"
        assert HookEvent.POST_TOOL_USE.value == "PostToolUse"
        assert HookEvent.POST_TOOL_USE_FAILURE.value == "PostToolUseFailure"

    @staticmethod
    def test_session_events():
        assert HookEvent.SESSION_START.value == "SessionStart"
        assert HookEvent.SESSION_END.value == "SessionEnd"

    @staticmethod
    def test_stop_event():
        assert HookEvent.STOP.value == "Stop"


class TestEventRouting:
    """测试事件分发：rail vs gateway."""

    @staticmethod
    def test_rail_events():
        assert is_rail_event(HookEvent.PRE_TOOL_USE) is True
        assert is_rail_event(HookEvent.POST_TOOL_USE) is True
        assert is_rail_event(HookEvent.POST_TOOL_USE_FAILURE) is True
        assert is_rail_event(HookEvent.STOP) is True
        assert is_rail_event(HookEvent.BEFORE_MODEL_CALL) is True
        assert is_rail_event(HookEvent.AFTER_MODEL_CALL) is True

    @staticmethod
    def test_gateway_events():
        assert is_gateway_event(HookEvent.SESSION_START) is True
        assert is_gateway_event(HookEvent.SESSION_END) is True
        assert is_gateway_event(HookEvent.USER_PROMPT_SUBMIT) is True
        assert is_gateway_event(HookEvent.NOTIFICATION) is True

    @staticmethod
    def test_no_overlap():
        """Rail 和 Gateway 事件不应重叠."""
        rail = {e for e in HookEvent if is_rail_event(e)}
        gateway = {e for e in HookEvent if is_gateway_event(e)}
        assert len(rail & gateway) == 0


class TestHookMatcherExact:
    """精确匹配测试."""

    @staticmethod
    def test_exact_match():
        m = HookMatcher(matcher="Write")
        assert m.matches("Write") is True

    @staticmethod
    def test_exact_no_match():
        m = HookMatcher(matcher="Write")
        assert m.matches("Bash") is False

    @staticmethod
    def test_exact_case_sensitive():
        m = HookMatcher(matcher="Write")
        assert m.matches("write") is False


class TestHookMatcherWildcard:
    """通配符匹配测试."""

    @staticmethod
    def test_star_matches_all():
        m = HookMatcher(matcher="*")
        assert m.matches("anything") is True
        assert m.matches("Write") is True
        assert m.matches("") is True

    @staticmethod
    def test_empty_string_matches_all():
        m = HookMatcher(matcher="")
        assert m.matches("anything") is True


class TestHookMatcherPipeOr:
    """管道 OR 匹配测试."""

    @staticmethod
    def test_pipe_or_match_first():
        m = HookMatcher(matcher="Write|Edit|Bash")
        assert m.matches("Write") is True

    @staticmethod
    def test_pipe_or_match_middle():
        m = HookMatcher(matcher="Write|Edit|Bash")
        assert m.matches("Edit") is True

    @staticmethod
    def test_pipe_or_match_last():
        m = HookMatcher(matcher="Write|Edit|Bash")
        assert m.matches("Bash") is True

    @staticmethod
    def test_pipe_or_no_match():
        m = HookMatcher(matcher="Write|Edit|Bash")
        assert m.matches("Read") is False

    @staticmethod
    def test_pipe_or_with_spaces():
        m = HookMatcher(matcher="Write | Edit | Bash")
        assert m.matches("Edit") is True


class TestHookMatcherRegex:
    """正则匹配测试."""

    @staticmethod
    def test_regex_prefix():
        m = HookMatcher(matcher="^Write.*")
        assert m.matches("WriteFile") is True
        assert m.matches("Write") is True

    @staticmethod
    def test_regex_suffix():
        m = HookMatcher(matcher=".*File$")
        assert m.matches("WriteFile") is True

    @staticmethod
    def test_regex_dot_star():
        m = HookMatcher(matcher="^Bash\\(.*\\)$")
        assert m.matches("Bash(git push)") is True

    @staticmethod
    def test_invalid_regex_returns_false():
        m = HookMatcher(matcher="[unclosed")
        assert m.matches("anything") is False


class TestHooksConfig:
    """HooksConfig 核心逻辑测试."""

    @staticmethod
    def _make_config(**events):
        matchers = {}
        for event_name, matcher_list in events.items():
            matchers[event_name] = [
                HookMatcher(matcher=m[0], hooks=m[1]) for m in matcher_list
            ]
        return HooksConfig(events=matchers)

    @staticmethod
    def test_match_returns_hooks_for_matching_event():
        config = TestHooksConfig._make_config(
            PreToolUse=[("Write", [{"type": "command", "command": "test"}])]
        )
        result = config.match("PreToolUse", query="Write")
        assert len(result) == 1
        assert result[0]["command"] == "test"

    @staticmethod
    def test_match_empty_for_non_matching_event():
        config = TestHooksConfig._make_config(
            PreToolUse=[("Write", [{"type": "command", "command": "test"}])]
        )
        result = config.match("PostToolUse", query="Write")
        assert result == []

    @staticmethod
    def test_match_empty_for_non_matching_matcher():
        config = TestHooksConfig._make_config(
            PreToolUse=[("Write", [{"type": "command", "command": "test"}])]
        )
        result = config.match("PreToolUse", query="Bash")
        assert result == []

    @staticmethod
    def test_multiple_hooks_returned():
        config = TestHooksConfig._make_config(
            PreToolUse=[
                ("Write", [
                    {"type": "command", "command": "hook1"},
                    {"type": "command", "command": "hook2"},
                ])
            ]
        )
        result = config.match("PreToolUse", query="Write")
        assert len(result) == 2

    @staticmethod
    def test_disable_all_hooks():
        config = TestHooksConfig._make_config(
            PreToolUse=[("*", [{"type": "command", "command": "test"}])]
        )
        config.disable_all_hooks = True
        assert config.match("PreToolUse", query="Write") == []

    @staticmethod
    def test_empty_config():
        config = HooksConfig()
        assert config.match("PreToolUse", query="Write") == []
        assert config.disable_all_hooks is False


class TestGetEventSummary:
    """get_event_summary 测试."""

    @staticmethod
    def test_returns_all_events():
        config = HooksConfig(events={
            "PreToolUse": [HookMatcher(matcher="*", hooks=[{"type": "command", "command": "test"}])]
        })
        summary = config.get_event_summary()
        assert len(summary) == 17  # all events

    @staticmethod
    def test_correct_hook_counts():
        config = HooksConfig(events={
            "PreToolUse": [
                HookMatcher(matcher="Write", hooks=[{"cmd": "a"}]),
                HookMatcher(matcher="Bash", hooks=[{"cmd": "b"}, {"cmd": "c"}]),
            ]
        })
        summary = config.get_event_summary()
        pre = [s for s in summary if s["name"] == "PreToolUse"][0]
        assert pre["total_hooks"] == 3
        assert len(pre["matchers"]) == 2

    @staticmethod
    def test_empty_config_returns_zero_counts():
        config = HooksConfig()
        summary = config.get_event_summary()
        for s in summary:
            assert s["total_hooks"] == 0


class TestLoadHooksConfig:
    """load_hooks_config 函数测试."""

    @staticmethod
    def test_empty_dict():
        config = load_hooks_config({})
        assert config.disable_all_hooks is False
        assert config.events == {}

    @staticmethod
    def test_none_config():
        config = load_hooks_config(None)
        assert config.disable_all_hooks is False

    @staticmethod
    def test_disable_all_flag():
        config = load_hooks_config({"hooks": {"disable_all_hooks": True}})
        assert config.disable_all_hooks is True

    @staticmethod
    def test_parses_event_configs():
        config = load_hooks_config({
            "hooks": {
                "PreToolUse": [
                    {
                        "matcher": "Write|Edit",
                        "hooks": [{"type": "command", "command": "echo test"}],
                    }
                ]
            }
        })
        assert "PreToolUse" in config.events
        assert config.events["PreToolUse"][0].matcher == "Write|Edit"
        result = config.match("PreToolUse", query="Write")
        assert len(result) == 1

    @staticmethod
    def test_skips_non_list_event():
        config = load_hooks_config({
            "hooks": {"PreToolUse": "not_a_list"}
        })
        assert config.events == {}

    @staticmethod
    def test_skips_non_dict_matcher_entry():
        config = load_hooks_config({
            "hooks": {
                "PreToolUse": ["not_a_dict", {"matcher": "*", "hooks": [{"type": "command", "command": "ok"}]}]
            }
        })
        assert "PreToolUse" in config.events
        assert len(config.events["PreToolUse"]) == 1  # only the dict entry


class TestCommandHookConfig:
    """CommandHookConfig 测试."""

    @staticmethod
    def test_defaults():
        c = CommandHookConfig()
        assert c.type == "command"
        assert c.timeout == 30
        assert c.shell == "bash"
        assert c.command == ""


class TestPromptHookConfig:
    """PromptHookConfig 测试."""

    @staticmethod
    def test_defaults():
        c = PromptHookConfig()
        assert c.type == "prompt"
        assert c.timeout == 15
        assert c.model == ""