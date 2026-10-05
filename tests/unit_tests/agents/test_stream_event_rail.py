# -*- coding: utf-8 -*-
"""stream_event_rail 工具结果错误判读测试（此前零直接测试）。

修复 #5019：``{"ok": False, "error": ...}`` 是仓库通用失败约定（sdd_advance
的 stage 拒绝、CLI JSON 输出、随包 skill 脚本、skill_manager 均使用），但
``_infer_tool_result_error`` 不读 ``ok`` 键——被拒绝的工具调用在事件流里
显示为成功（web 时间线 / TUI / 调试 trace 全部误报）。

- ``ok`` 组：修复前红，修复后绿；
- 既有约定组（success / is_error / status / 退出码 / 嵌套 / 字符串）作回归锁；
- e2e：issue 的最小复现——生产入口 after_tool_call + 真实
  RailStateMachineBase 拒绝值，断言事件携带失败判读。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.code.rails.sdd.common.rail_state_machine import (
    RailStateMachineBase,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    JiuSwarmStreamEventRail,
    _infer_tool_result_error,
)
from openjiuwen.core.single_agent.rail.base import (
    AgentCallbackContext,
    ToolCallInputs,
)

pytestmark = [pytest.mark.unit]


class _StubSession:
    """记录 write_stream 事件的会话替身。"""

    def __init__(self) -> None:
        self.events: list = []

    async def write_stream(self, event) -> None:
        self.events.append(event)


# ── ok 约定（修复目标）──


def test_ok_false_with_error_is_error() -> None:
    """sdd_advance 拒绝形态 {"ok": False, "error": ...} 必须判为失败。"""
    assert _infer_tool_result_error({"ok": False, "error": "invalid stage: 'design'"}) is True


def test_ok_false_alone_is_error() -> None:
    assert _infer_tool_result_error({"ok": False}) is True


@pytest.mark.parametrize("key", ["data", "raw_output", "rawOutput", "result"])
def test_ok_false_in_nested_payload_is_error(key: str) -> None:
    """嵌套载荷里的 ok: False 同样生效（与其他约定同一递归路径）。"""
    assert _infer_tool_result_error({key: {"ok": False}}) is True


def test_ok_false_json_string_result_is_error() -> None:
    assert _infer_tool_result_error('{"ok": false, "error": "boom"}') is True


def test_ok_false_text_form_is_error() -> None:
    """非 JSON 文本中的 ok: False（与既有 success 文本正则对称）。"""
    assert _infer_tool_result_error("step ok: False, see log") is True


def test_ok_true_does_not_mask_other_failure_signals() -> None:
    """ok: True 不判定成功——退出码等其他失败信号继续参与判读。"""
    assert _infer_tool_result_error({"ok": True, "exit_code": 1}) is True


def test_ok_true_alone_has_no_verdict() -> None:
    """ok: True 单独出现不下结论（保守：只修漏报，不扩大判定面）。"""
    assert _infer_tool_result_error({"ok": True}) is None


def test_success_wins_over_ok_precedence() -> None:
    """success 优先于 ok：两者并存时以更明确的 success 为准。"""
    assert _infer_tool_result_error({"success": True, "ok": False}) is False


# ── 既有约定回归锁 ──


def test_success_false_is_error_and_true_is_success() -> None:
    assert _infer_tool_result_error({"success": False}) is True
    assert _infer_tool_result_error({"success": True}) is False


def test_is_error_flags() -> None:
    assert _infer_tool_result_error({"is_error": True}) is True
    assert _infer_tool_result_error({"isError": True}) is True


def test_status_error_words() -> None:
    assert _infer_tool_result_error({"status": "error"}) is True
    assert _infer_tool_result_error({"status": "FAILED"}) is True
    assert _infer_tool_result_error({"status": "ok"}) is None


def test_exit_code_conventions() -> None:
    assert _infer_tool_result_error({"exit_code": 2}) is True
    assert _infer_tool_result_error({"exit_code": 0}) is False
    assert _infer_tool_result_error({"returncode": "1"}) is True


def test_nested_success_false_is_error() -> None:
    assert _infer_tool_result_error({"data": {"success": False}}) is True


def test_list_with_error_item_is_error() -> None:
    assert _infer_tool_result_error([{"success": True}, {"success": False}]) is True


def test_list_with_ok_false_item_is_error() -> None:
    assert _infer_tool_result_error([{"ok": False}]) is True


def test_string_json_success_false_is_error() -> None:
    assert _infer_tool_result_error('{"success": false}') is True


def test_string_error_prefix_is_error() -> None:
    assert _infer_tool_result_error("[ERROR] command failed") is True


def test_string_success_false_text_is_error() -> None:
    assert _infer_tool_result_error("result: success: False") is True


def test_plain_string_has_no_verdict() -> None:
    assert _infer_tool_result_error("all good") is None


def test_object_success_attribute() -> None:
    assert _infer_tool_result_error(SimpleNamespace(success=False)) is True
    assert _infer_tool_result_error(SimpleNamespace(success=True)) is False


# ── e2e：issue 的最小复现（生产入口 + 真实拒绝值）──


@pytest.mark.asyncio
async def test_after_tool_call_emits_failure_verdict_for_sdd_advance_refusal() -> None:
    """被 sdd_advance 拒绝的调用，事件必须携带 success/status/is_error 失败判读。"""
    refusal = RailStateMachineBase(
        rail_pkg_dir=Path("."), project_dir=Path(".")
    )._handle_advance({"stage": "design"})
    assert refusal["ok"] is False  # 前置：确为失败结果

    session = _StubSession()
    call = SimpleNamespace(name="sdd_advance", id="tc-1", arguments={"stage": "design"})
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=ToolCallInputs(
            tool_call=call, tool_name="sdd_advance", tool_args={"stage": "design"}
        ),
        extra={},
    )
    ctx.inputs.tool_result = refusal
    ctx.session = session

    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    assert len(session.events) == 1
    payload = session.events[0].payload["tool_result"]
    assert payload["success"] is False
    assert payload["status"] == "error"
    assert payload["is_error"] is True
    assert payload["raw_output"] == refusal  # 原始结果原样透出


@pytest.mark.asyncio
async def test_after_tool_call_emits_success_verdict_for_success_result() -> None:
    session = _StubSession()
    call = SimpleNamespace(name="bash", id="tc-2", arguments={"cmd": "ls"})
    ctx = AgentCallbackContext(
        agent=SimpleNamespace(),
        inputs=ToolCallInputs(tool_call=call, tool_name="bash", tool_args={"cmd": "ls"}),
        extra={},
    )
    ctx.inputs.tool_result = {"success": True, "output": "a.txt"}
    ctx.session = session

    await JiuSwarmStreamEventRail().after_tool_call(ctx)

    payload = session.events[0].payload["tool_result"]
    assert payload["success"] is True
    assert "status" not in payload  # 成功不写 error 字段
