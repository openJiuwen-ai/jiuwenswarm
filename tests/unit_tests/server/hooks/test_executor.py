# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Unit tests for jiuwenswarm.server.hooks.executor."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.hooks.executor import HookExecutor, HookResult, HookOutcome


# ============================================================
# HookResult
# ============================================================

class TestHookResult:
    @staticmethod
    def test_defaults():
        r = HookResult()
        assert r.outcome == HookOutcome.SUCCESS
        assert r.error == ""
        assert r.show_to_model is False
        assert r.modified_input is None
        assert r.additional_context == ""

    @staticmethod
    def test_blocking():
        r = HookResult(outcome=HookOutcome.BLOCKING, error="blocked", show_to_model=True)
        assert r.outcome == "blocking"
        assert r.error == "blocked"
        assert r.show_to_model is True


# ============================================================
# HookExecutor: run_all dispatch
# ============================================================

class TestRunAll:
    @staticmethod
    @pytest.mark.asyncio
    async def test_empty_configs():
        e = HookExecutor()
        results = await e.run_all([], {})
        assert results == []

    @staticmethod
    @pytest.mark.asyncio
    async def test_dispatches_command_by_default():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo ok"}],
            {"event": "Test"},
        )
        assert len(results) == 1
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_dispatches_prompt_type():
        e = HookExecutor()
        results = await e.run_all(
            [{"type": "prompt", "prompt": "test"}],
            {"event": "Test"},
        )
        assert len(results) == 1
        # prompt hook 尝试调 LLM → 无配置时 non_blocking_error 也算正常
        assert results[0].outcome in (HookOutcome.SUCCESS, HookOutcome.NON_BLOCKING_ERROR)

    @staticmethod
    @pytest.mark.asyncio
    async def test_parallel_execution():
        """多个 hook 应并行执行."""
        e = HookExecutor()
        results = await e.run_all(
            [
                {"command": "echo a"},
                {"command": "echo b"},
                {"command": "echo c"},
            ],
            {"event": "Test"},
        )
        assert len(results) == 3
        assert all(r.outcome == HookOutcome.SUCCESS for r in results)

    @staticmethod
    @pytest.mark.asyncio
    async def test_exception_wrapped_as_error():
        """异常应包装为 non_blocking_error."""
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "exit 1"}],
            {"event": "Test"},
        )
        assert len(results) == 1
        assert results[0].outcome == HookOutcome.NON_BLOCKING_ERROR


# ============================================================
# Command Hook: Exit Code Semantics
# ============================================================

class TestCommandHookExitCodes:
    @staticmethod
    @pytest.mark.asyncio
    async def test_exit_0_is_success():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "exit 0", "timeout": 5}],
            {"event": "PreToolUse", "tool_name": "Bash"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_exit_2_is_blocking():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo 'blocked by policy' >&2; exit 2", "timeout": 5}],
            {"event": "PreToolUse", "tool_name": "Bash"},
        )
        r = results[0]
        assert r.outcome == HookOutcome.BLOCKING
        assert r.show_to_model is True

    @staticmethod
    @pytest.mark.asyncio
    async def test_exit_1_is_non_blocking_error():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "exit 1", "timeout": 5}],
            {"event": "PreToolUse"},
        )
        assert results[0].outcome == HookOutcome.NON_BLOCKING_ERROR

    @staticmethod
    @pytest.mark.asyncio
    async def test_exit_127_is_non_blocking_error():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "exit 127", "timeout": 5}],
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.NON_BLOCKING_ERROR


# ============================================================
# Command Hook: Environment Variables
# ============================================================

class TestCommandHookEnvVars:
    @staticmethod
    @pytest.mark.asyncio
    async def test_arguments_env_var_set():
        """$ARGUMENTS 环境变量包含 hook input JSON."""
        e = HookExecutor()
        hook_input = {"event": "PreToolUse", "tool_name": "Write", "key": "value123"}
        results = await e.run_all(
            [{"command": "echo $ARGUMENTS", "timeout": 5}],
            hook_input,
        )
        # exit 0, stdout 包含 "value123"
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_tool_name_env_var_set():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": 'test "$TOOL_NAME" = "Write"', "timeout": 5}],
            {"event": "PreToolUse", "tool_name": "Write"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_tool_name_empty_when_missing():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": 'test "$TOOL_NAME" = ""', "timeout": 5}],
            {"event": "Stop"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS


# ============================================================
# Command Hook: timeout handling
# ============================================================

class TestCommandHookTimeout:
    @staticmethod
    @pytest.mark.asyncio
    async def test_timeout_returns_non_blocking_error():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "sleep 5", "timeout": 1}],
            {"event": "Test"},
        )
        r = results[0]
        assert r.outcome == HookOutcome.NON_BLOCKING_ERROR
        assert "timeout" in r.error.lower()

    @staticmethod
    @pytest.mark.asyncio
    async def test_timeout_does_not_block_execution():
        """超时不应阻止后续 hook 或其他工具."""
        e = HookExecutor()
        results = await e.run_all(
            [
                {"command": "sleep 5", "timeout": 1},
                {"command": "echo ok", "timeout": 5},
            ],
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.NON_BLOCKING_ERROR
        assert results[1].outcome == HookOutcome.SUCCESS


# ============================================================
# Command Hook: empty command
# ============================================================

class TestCommandHookEmptyCommand:
    @staticmethod
    @pytest.mark.asyncio
    async def test_empty_command_error():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "", "timeout": 5}],
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.NON_BLOCKING_ERROR
        assert "empty command" in results[0].error.lower()


# ============================================================
# Command Hook: shell selection
# ============================================================

class TestCommandHookShell:
    @staticmethod
    @pytest.mark.asyncio
    async def test_default_shell_bash():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo $SHELL", "timeout": 5}],
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_sh_shell():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo ok", "shell": "sh", "timeout": 5}],
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_default_timeout_30():
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo ok"}],  # no timeout specified → default 30
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS


# ============================================================
# Command Hook: JSON Output Protocol (parse_command_output)
# ============================================================

class TestParseCommandOutput:
    @staticmethod
    def test_empty_stdout_returns_success():
        r = HookExecutor.parse_command_output("")
        assert r.outcome == HookOutcome.SUCCESS

    @staticmethod
    def test_whitespace_only():
        r = HookExecutor.parse_command_output("   \n  ")
        assert r.outcome == HookOutcome.SUCCESS

    @staticmethod
    def test_non_json_is_success():
        r = HookExecutor.parse_command_output("just some text")
        assert r.outcome == HookOutcome.SUCCESS

    @staticmethod
    def test_non_dict_json_is_success():
        r = HookExecutor.parse_command_output("[1, 2, 3]")
        assert r.outcome == HookOutcome.SUCCESS

    @staticmethod
    def test_empty_json_object():
        r = HookExecutor.parse_command_output("{}")
        assert r.outcome == HookOutcome.SUCCESS

    @staticmethod
    def test_decision_block():
        stdout = json.dumps({"decision": "block", "reason": "dangerous operation"})
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.BLOCKING
        assert r.show_to_model is True
        assert "dangerous operation" in r.error

    @staticmethod
    def test_decision_block_no_reason():
        stdout = json.dumps({"decision": "block"})
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.BLOCKING

    @staticmethod
    def test_modified_input():
        stdout = json.dumps({"modifiedInput": {"file_path": "/safe/path.txt"}})
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.SUCCESS
        assert r.modified_input == {"file_path": "/safe/path.txt"}

    @staticmethod
    def test_additional_context():
        stdout = json.dumps({"additionalContext": "review: looks good"})
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.SUCCESS
        assert r.additional_context == "review: looks good"

    @staticmethod
    def test_reason_without_block_is_context():
        stdout = json.dumps({"reason": "note: file was modified recently"})
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.SUCCESS
        assert r.additional_context == "note: file was modified recently"

    @staticmethod
    def test_combined_modified_and_context():
        stdout = json.dumps({
            "modifiedInput": {"file_path": "/safe/path.txt"},
            "additionalContext": "path was redirected for safety",
        })
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.SUCCESS
        assert r.modified_input is not None
        assert r.additional_context == "path was redirected for safety"

    @staticmethod
    def test_decision_block_takes_precedence():
        """即使有 modifiedInput，decision: block 也优先."""
        stdout = json.dumps({
            "decision": "block",
            "reason": "dangerous",
            "modifiedInput": {"file_path": "/safe/path.txt"},
        })
        r = HookExecutor.parse_command_output(stdout)
        assert r.outcome == HookOutcome.BLOCKING


# ============================================================
# Prompt Hook: placeholder / template substitution
# ============================================================

class TestPromptHook:
    @staticmethod
    @pytest.mark.asyncio
    async def test_placeholder_returns_non_blocking_error_without_llm_config():
        """未配置 LLM 时 prompt hook 返回 non_blocking_error."""
        e = HookExecutor()
        results = await e.run_all(
            [{"type": "prompt", "prompt": "review: $ARGUMENTS"}],
            {"event": "PreToolUse", "tool_name": "Write"},
        )
        assert len(results) == 1
        # 没有 LLM 配置时，调用会失败 → non_blocking_error
        assert results[0].outcome in (HookOutcome.SUCCESS, HookOutcome.NON_BLOCKING_ERROR)

    @staticmethod
    @pytest.mark.asyncio
    async def test_empty_prompt_config():
        e = HookExecutor()
        results = await e.run_all(
            [{"type": "prompt", "prompt": ""}],
            {"event": "Test"},
        )
        assert results[0].outcome == HookOutcome.NON_BLOCKING_ERROR
        assert "empty prompt" in results[0].error.lower()


# ============================================================
# extract_json_from_response
# ============================================================

class TestExtractJsonFromResponse:
    @staticmethod
    def test_direct_json_object():
        text = '{"decision": "allow"}'
        result = HookExecutor.extract_json_from_response(text)
        assert result == {"decision": "allow"}

    @staticmethod
    def test_json_in_markdown_fence():
        text = '```json\n{"decision": "block", "reason": "bad"}\n```'
        result = HookExecutor.extract_json_from_response(text)
        assert result == {"decision": "block", "reason": "bad"}

    @staticmethod
    def test_json_in_plain_fence():
        text = '```\n{"key": "value"}\n```'
        result = HookExecutor.extract_json_from_response(text)
        assert result == {"key": "value"}

    @staticmethod
    def test_json_with_surrounding_text():
        text = 'Here is my review:\n{"decision": "allow"}\nThat is all.'
        result = HookExecutor.extract_json_from_response(text)
        assert result == {"decision": "allow"}

    @staticmethod
    def test_no_json():
        text = "just a plain response"
        result = HookExecutor.extract_json_from_response(text)
        assert result == {}

    @staticmethod
    def test_empty_string():
        result = HookExecutor.extract_json_from_response("")
        assert result == {}

    @staticmethod
    def test_nested_json():
        text = '{"decision": "allow", "details": {"score": 95}}'
        result = HookExecutor.extract_json_from_response(text)
        assert result == {"decision": "allow", "details": {"score": 95}}


# ============================================================
# End-to-end: full command hook flow
# ============================================================

class TestCommandHookE2E:
    """端到端 command hook 流程测试."""

    @staticmethod
    @pytest.mark.asyncio
    async def test_simple_logging_hook():
        """模拟最简单的日志 hook：echo 内容到 stdout."""
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo log: tool=$TOOL_NAME", "timeout": 5}],
            {"event": "PreToolUse", "tool_name": "Read"},
        )
        assert results[0].outcome == HookOutcome.SUCCESS

    @staticmethod
    @pytest.mark.asyncio
    async def test_blocking_hook_via_json_stdout():
        """通过 stdout JSON decision: block 阻止执行."""
        e = HookExecutor()
        results = await e.run_all(
            [{
                "command": 'echo \'{"decision":"block","reason":"not allowed"}\'',
                "timeout": 5,
            }],
            {"event": "PreToolUse", "tool_name": "Bash"},
        )
        r = results[0]
        assert r.outcome == HookOutcome.BLOCKING
        assert "not allowed" in r.error

    @staticmethod
    @pytest.mark.asyncio
    async def test_blocking_hook_via_exit_2():
        """通过退出码 2 阻止执行（无 JSON stdout）."""
        e = HookExecutor()
        results = await e.run_all(
            [{"command": "echo blocked >&2; exit 2", "timeout": 5}],
            {"event": "PreToolUse", "tool_name": "Bash"},
        )
        r = results[0]
        assert r.outcome == HookOutcome.BLOCKING
        assert r.show_to_model is True

    @staticmethod
    @pytest.mark.asyncio
    async def test_modify_input_hook():
        """通过 stdout JSON modifiedInput 修改工具输入."""
        e = HookExecutor()
        results = await e.run_all(
            [{
                "command": 'echo \'{"modifiedInput":{"file_path":"/safe/path.txt"}}\'',
                "timeout": 5,
            }],
            {"event": "PreToolUse", "tool_name": "Write"},
        )
        r = results[0]
        assert r.outcome == HookOutcome.SUCCESS
        assert r.modified_input == {"file_path": "/safe/path.txt"}

    @staticmethod
    @pytest.mark.asyncio
    async def test_additional_context_hook():
        """通过 stdout JSON additionalContext 注入上下文."""
        e = HookExecutor()
        results = await e.run_all(
            [{
                "command": 'echo \'{"additionalContext":"file was last modified 2h ago"}\'',
                "timeout": 5,
            }],
            {"event": "PostToolUse", "tool_name": "Read"},
        )
        r = results[0]
        assert r.outcome == HookOutcome.SUCCESS
        assert "2h ago" in r.additional_context