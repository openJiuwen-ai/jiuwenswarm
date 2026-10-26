# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for tool_utils bash 解析机器（自 ppt 范例 bash_utils 上收）。

背景（dev-stable 运行时 ground truth）：bash 工具只回
``ToolOutput(success=True, data={"content": 合并输出文本})``，
**不带** stdout/stderr/exit_code 字段；exit_code 由失败信号反推
（success=False / error 非空 / 输出含 ``[ERROR]`` / 文本含 ``Exit code N``）。

红线（本文件逐条锁定）：
- 成功 bash（success=True + content）不得因缺 exit_code 字段被误判失败；
- 失败信号必须把 exit_code 从 0 升为非零（1 或文本中的 N）；
- ``run_bash`` 返回 ``BashResult``，getter（get_tool_exit_code 等）对其
  同样正确（_extract_bash_result 的 BashResult 守卫，防文本兜底恒返 0）；
- ``required=True`` 非零退出码抛 ``BashExecError``；AbortError 透传；
  第一次 ValueError（不支持 timeout 参数的注册形态）降级重试。
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.plan_node import AbortError
from jiuwenswarm.server.runtime.skill_turbo.runtime.tool_utils import (
    BashExecError,
    BashResult,
    combined_output,
    get_tool_data,
    get_tool_exit_code,
    get_tool_stderr,
    get_tool_stdout,
    parse_bash_payload,
    parse_bash_result,
    quote_path,
    run_bash,
)


class _FakeNode:
    """按脚本顺序执行 call_tool 的桩节点。"""

    def __init__(self, script: list[Any] | None = None) -> None:
        self.script = list(script or [])
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call_tool(self, name: str, **kwargs: Any) -> Any:
        self.calls.append((name, kwargs))
        action = self.script.pop(0) if self.script else {"content": ""}
        if isinstance(action, BaseException):
            raise action
        return action


def _ok(content: str = "ok") -> SimpleNamespace:
    """dev-stable bash 工具成功形态：success=True + data={"content"}。"""
    return SimpleNamespace(success=True, data={"content": content})


# ── get_tool_exit_code：失败信号反推 ──────────────────────────────────


def test_exit_code_success_content_only_is_zero():
    # 真实形态：无 exit_code 字段，success=True 覆盖调用方 default=1
    assert get_tool_exit_code(_ok("hello"), default=1) == 0


def test_exit_code_success_false_error_attr_is_one():
    result = SimpleNamespace(success=False, data=None, error="boom")
    assert get_tool_exit_code(result, default=1) == 1


def test_exit_code_error_marker_in_content_is_one():
    result = SimpleNamespace(success=True, data={"content": "[ERROR]: something failed"})
    assert get_tool_exit_code(result, default=1) == 1


def test_exit_code_exit_code_line_inferred():
    result = _ok("done\nExit code 3\nremaining output")
    assert get_tool_exit_code(result, default=1) == 3


def test_exit_code_explicit_nonzero_kept():
    # grep 形态：显式 exit_code=1（无匹配），success=True 也如实返回 1
    result = SimpleNamespace(
        success=True, data={"stdout": "", "stderr": "", "exit_code": 1, "mode": "content"}
    )
    assert get_tool_exit_code(result, default=1) == 1
    result2 = SimpleNamespace(success=True, data={"stdout": "o", "stderr": "e", "exit_code": 2})
    assert get_tool_exit_code(result2, default=1) == 2


def test_exit_code_none_falls_back_to_default():
    assert get_tool_exit_code(None) == 1
    assert get_tool_exit_code(None, default=7) == 7


def test_exit_code_empty_text_falls_back_to_default():
    # 完全无信息（空 content）才回落 default
    assert get_tool_exit_code({"content": ""}, default=5) == 5


# ── BashResult 守卫：getter 对 run_bash 返回值同样正确 ────────────────


def test_getters_accept_bash_result_directly():
    # 修复前：BashResult 无 .data → 文本兜底 → get_tool_exit_code 恒返 0
    br = BashResult(exit_code=3, stdout="out", stderr="err", raw="raw")
    assert get_tool_exit_code(br) == 3
    assert get_tool_stdout(br) == "out"
    assert get_tool_stderr(br) == "err"
    assert parse_bash_result(br) is br


def test_getters_accept_bash_result_zero():
    br = BashResult(exit_code=0, stdout="out", stderr="", raw="raw")
    assert get_tool_exit_code(br, default=1) == 0


# ── get_tool_stdout / get_tool_stderr：回退链 ─────────────────────────


def test_stdout_prefers_stdout_then_content():
    assert get_tool_stdout(SimpleNamespace(success=True, data={"stdout": "real"})) == "real"
    assert get_tool_stdout(_ok("from content")) == "from content"
    assert get_tool_stdout(SimpleNamespace(success=True, data={"output": "via output"})) == "via output"


def test_stdout_plain_dict_and_none():
    assert get_tool_stdout({"content": "plain dict"}) == "plain dict"
    assert get_tool_stdout(None) == ""


def test_stderr_from_data_and_error_prefix():
    assert get_tool_stderr(SimpleNamespace(success=False, data={"stderr": "warn"})) == "warn"
    assert get_tool_stderr(SimpleNamespace(success=False, data=None, error="boom")) == "[ERROR]: boom"


def test_stderr_success_content_is_empty():
    # 成功调用的 content 不是 stderr
    assert get_tool_stderr(_ok("normal output")) == ""


# ── get_tool_data：公开 data 提取入口 ────────────────────────────────


def test_get_data_tooloutput_dict_and_plain_dict():
    assert get_tool_data(SimpleNamespace(success=True, data={"replacements": 2})) == {"replacements": 2}
    assert get_tool_data({"a": 1}) == {"a": 1}
    assert get_tool_data(None) == {}
    assert get_tool_data(BashResult(exit_code=0, stdout="", stderr="", raw="")) == {}


def test_get_data_field_access_pattern():
    # schema 推荐用法：get_tool_data(result).get("replacements", 0)
    assert get_tool_data(SimpleNamespace(success=True, data={"replacements": 3})).get("replacements", 0) == 3


# ── parse_bash_payload / parse_bash_result：文本形态 ─────────────────


def test_parse_payload_json():
    parsed = parse_bash_payload('{"exit_code": 0, "stdout": "ok", "stderr": ""}')
    assert (parsed.exit_code, parsed.stdout) == (0, "ok")


def test_parse_payload_exit_code_prefix():
    parsed = parse_bash_payload("Exit code 127\ncommand not found")
    assert parsed.exit_code == 127
    assert "command not found" in parsed.stdout


def test_parse_payload_error_prefix():
    parsed = parse_bash_payload("[ERROR]: tool failed")
    assert parsed.exit_code == 1
    assert parsed.stderr.startswith("[ERROR]")


def test_parse_payload_plain_text_is_zero():
    parsed = parse_bash_payload("plain output")
    assert parsed.exit_code == 0
    assert parsed.stdout == "plain output"


def test_parse_result_structured_priority():
    parsed = parse_bash_result(_ok("structured wins"))
    assert parsed.exit_code == 0
    assert parsed.stdout == "structured wins"


# ── quote_path / combined_output ─────────────────────────────────────


def test_quote_path_escapes_and_wraps():
    assert quote_path("plain") == '"plain"'
    assert quote_path('has "quote"') == '"has \\"quote\\""'


def test_combined_output():
    assert combined_output(BashResult(exit_code=0, stdout="out", stderr="err", raw="")) == "out\nerr"
    assert combined_output(BashResult(exit_code=0, stdout="only", stderr="", raw="")) == "only"


# ── run_bash：执行 + 解析一体 ─────────────────────────────────────────


@pytest.mark.asyncio
async def test_run_bash_success_returns_bash_result():
    node = _FakeNode(script=[_ok("hello")])
    result = await run_bash(node, "echo hello")
    assert isinstance(result, BashResult)
    assert result.exit_code == 0
    assert result.stdout == "hello"
    # 第一次尝试带 timeout 参数
    assert "timeout" in node.calls[0][1]


@pytest.mark.asyncio
async def test_run_bash_required_false_returns_nonzero():
    node = _FakeNode(script=[_ok("Exit code 3\nsome error")])
    result = await run_bash(node, "failing", required=False)
    assert result.exit_code == 3


@pytest.mark.asyncio
async def test_run_bash_required_true_raises_on_nonzero():
    node = _FakeNode(script=[_ok("Exit code 3\nsome error")])
    with pytest.raises(BashExecError, match="exit=3"):
        await run_bash(node, "failing", required=True)


@pytest.mark.asyncio
async def test_run_bash_abort_error_propagates():
    node = _FakeNode(script=[AbortError("hitl interrupt")])
    with pytest.raises(AbortError):
        await run_bash(node, "anything", required=False)
    # AbortError 透传且不降级重试：仅一次调用
    assert len(node.calls) == 1


@pytest.mark.asyncio
async def test_run_bash_valueerror_falls_back_without_timeout():
    # 部分 bash 工具注册形态不支持 timeout 参数：ValueError 后降级重试
    node = _FakeNode(script=[ValueError("unexpected keyword 'timeout'"), _ok("recovered")])
    result = await run_bash(node, "echo hi")
    assert result.exit_code == 0
    assert result.stdout == "recovered"
    assert len(node.calls) == 2
    assert "timeout" in node.calls[0][1]
    assert "timeout" not in node.calls[1][1]


@pytest.mark.asyncio
async def test_run_bash_all_attempts_fail_raises():
    node = _FakeNode(
        script=[
            RuntimeError("attempt 1"),
            RuntimeError("attempt 2"),
        ]
    )
    with pytest.raises(BashExecError, match="无法执行 bash 命令"):
        await run_bash(node, "broken", required=False)
    assert len(node.calls) == 2


@pytest.mark.asyncio
async def test_run_bash_workdir_passed_through():
    node = _FakeNode(script=[_ok("ok")])
    await run_bash(node, "pwd", workdir="D:/tmp")
    assert node.calls[0][1]["workdir"] == "D:/tmp"
