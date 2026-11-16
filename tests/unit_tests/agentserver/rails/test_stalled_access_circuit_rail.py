# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tests for the strategy-level auth/credential loop breaker."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest
from jiuwenswarm.agents.harness.common.rails.execution_guard.stalled_access_circuit_rail import (
    StalledAccessCircuitRail,
)
from openjiuwen.core.foundation.llm import ToolMessage
from openjiuwen.core.single_agent.rail.base import ToolCallInputs


def _ctx(tool_result, tool_args=None, tool_name="bash"):
    args = tool_args or {}
    return SimpleNamespace(
        session=SimpleNamespace(get_session_id=lambda: "session-1"),
        inputs=ToolCallInputs(
            tool_call=SimpleNamespace(id="call-1", name=tool_name, arguments=args),
            tool_name=tool_name,
            tool_args=args,
            tool_result=tool_result,
            tool_msg=ToolMessage(content="", tool_call_id="call-1"),
        ),
        request_force_finish=MagicMock(),
    )


async def _assert_soft_refusal_then_answer(rail, ctx) -> None:
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()
    assert str(ctx.inputs.tool_result).startswith("[ERROR]:")
    assert ctx.inputs.tool_msg.metadata.get("is_error") is True

    # 模拟下一次工具调用返回了新的原始结果；hard-stop 也必须改写它。
    ctx.inputs.tool_result = {"content": "raw credential output"}
    ctx.inputs.tool_msg.content = "raw credential output"
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_called_once()
    assert str(ctx.inputs.tool_result).startswith("[ERROR]:")
    assert "raw credential output" not in str(ctx.inputs.tool_result)
    assert ctx.inputs.tool_msg.content.startswith("[ERROR]:")
    assert ctx.inputs.tool_msg.metadata.get("is_error") is True
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "answer"
    assert "凭证" in payload["output"]


@pytest.mark.asyncio
async def test_trips_after_repeated_auth_failures() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=3, probe_threshold=99)
    ctx = _ctx({"content": "HTTP 401 AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "error"
    assert "重新登录" in payload["output"]


@pytest.mark.asyncio
async def test_trips_after_repeated_credential_probes() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=2)
    ctx = _ctx(
        {"content": "wrote copy_cookies_share.py"},
        {"content": "import ctypes; crypt32.CryptUnprotectData(data)"},
    )
    await _assert_soft_refusal_then_answer(rail, ctx)


@pytest.mark.asyncio
async def test_hard_stop_rewrites_threshold_probe_result() -> None:
    # 复现 7492 会话：同一轮第 3 次探测达到阈值时，最后一次工具结果也必须
    # 被改写，不能把真实读取结果留在模型上下文 / history 里。
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=3)
    ctx = _ctx({"content": "ok"}, {"command": 'echo "Network/Cookies"'})
    await rail.before_invoke(ctx)

    for index in range(2):
        ctx.inputs.tool_result = {"content": f"raw-probe-{index}"}
        ctx.inputs.tool_msg.content = f"raw-probe-{index}"
        await rail.after_tool_call(ctx)
        ctx.request_force_finish.assert_not_called()
        assert str(ctx.inputs.tool_result).startswith("[ERROR]:")

    ctx.inputs.tool_result = {"content": "raw-credential-output"}
    ctx.inputs.tool_msg.content = "raw-credential-output"
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "answer"
    assert str(ctx.inputs.tool_result).startswith("[ERROR]:")
    assert "raw-credential-output" not in str(ctx.inputs.tool_result)
    assert ctx.inputs.tool_msg.content.startswith("[ERROR]:")
    assert ctx.inputs.tool_msg.metadata.get("is_error") is True


@pytest.mark.asyncio
async def test_dpapi_mentions_in_diagnostics_do_not_count_as_probes() -> None:
    # 真实事故回归：用户让 agent 分析 "native DPAPI helper failed"，agent 用
    # grep / memory / rg 搜该字符串。命中的只是错误文案，不是读取凭证。
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=3)
    ctx = _ctx(
        {
            "content": "7452784:native DPAPI helper failed\n8088696:native DPAPI helper failed"
        },
        {"pattern": "native DPAPI helper failed", "path": "workspace"},
        tool_name="grep",
    )
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    ctx.inputs.tool_name = "invoke_tool"
    ctx.inputs.tool_args = {"query": "OfficeAce 登录失败 DPAPI native helper 错误排查"}
    await rail.after_tool_call(ctx)
    ctx.inputs.tool_name = "bash"
    ctx.inputs.tool_args = {
        "command": "rg -a -o -b --no-heading 'native DPAPI helper failed' resources/app.asar"
    }
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_dpapi_source_snippet_does_not_count_as_probe() -> None:
    # 读取 app.asar 中的 DPAPI 模块源码不属于读取凭证；源码里的
    # OC-DPAPI-1 / ProtectedData]::Protect / dpapi-unprotect 都是普通文本。
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    source = (
        "DPAPI 文件加密：把本地根密钥交由 Windows DPAPI 托管。\n"
        "OC-DPAPI-1\n"
        "$enc=[Security.Cryptography.ProtectedData]::Protect($bytes,$null,$scope)\n"
        "runDpapiAsync('dpapi-unprotect', blob)"
    )
    ctx = _ctx({"content": source}, {"command": "node -e 'read app.asar'"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_result_side_credential_text_never_rewrites_tool_output() -> None:
    # 读取本仓 rail 源码或 DPAPI 错误日志时，结果正文会包含 session.cookie /
    # CryptUnprotectData 等字样；这不是凭证访问，不能被软拒绝吞掉。
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    result = {
        "content": (
            "stalled_access_circuit_rail.py: session.cookie; "
            "native DPAPI helper failed: CryptUnprotectData"
        )
    }
    ctx = _ctx(result, {"path": "stalled_access_circuit_rail.py"}, tool_name="read_file")
    await rail.before_invoke(ctx)

    for _ in range(3):
        await rail.after_tool_call(ctx)
        ctx.request_force_finish.assert_not_called()
        assert ctx.inputs.tool_result is result
        assert ctx.inputs.tool_msg.content == ""

    assert rail._result_probe_hits == 3


@pytest.mark.asyncio
async def test_explicit_dpapi_helper_invocation_still_counts() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    ctx = _ctx(
        {"content": "ok"},
        {"command": "OfficeAceServiceHost.exe --mode dpapi-unprotect"},
    )
    await _assert_soft_refusal_then_answer(rail, ctx)


@pytest.mark.asyncio
async def test_explicit_dpapi_helper_equals_form_still_counts() -> None:
    # --mode=dpapi-unprotect 与 --mode dpapi-unprotect 等价，不能漏计。
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    ctx = _ctx(
        {"content": "ok"},
        {"command": "OfficeAceServiceHost.exe --mode=dpapi-unprotect"},
    )
    await _assert_soft_refusal_then_answer(rail, ctx)


@pytest.mark.asyncio
async def test_explicit_dpapi_helper_quoted_value_still_counts() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    ctx = _ctx(
        {"content": "ok"},
        {"command": 'OfficeAceServiceHost.exe --mode "dpapi-unprotect"'},
    )
    await _assert_soft_refusal_then_answer(rail, ctx)


@pytest.mark.asyncio
async def test_explicit_dpapi_helper_argv_list_still_counts() -> None:
    # exec 类工具的 args 可能是 argv 数组；扁平化后必须仍能看到
    # "--mode" 与 "dpapi-unprotect" 这对相邻参数。
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    ctx = _ctx(
        {"content": "ok"},
        {"argv": ["OfficeAceServiceHost.exe", "--mode", "dpapi-unprotect"]},
    )
    await _assert_soft_refusal_then_answer(rail, ctx)


@pytest.mark.asyncio
async def test_single_auth_failure_does_not_trip() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=3, probe_threshold=99)
    ctx = _ctx({"content": "401 AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_normal_tool_results_do_not_count_as_probes() -> None:
    # Both tool_result and tool_args participate in the scan; "ok" contains
    # no credential fingerprint and therefore must not add to the count.
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=3)
    ctx = _ctx({"content": "ok"}, {})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    ctx.inputs.tool_args = {"content": "CryptUnprotectData"}
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    ctx.inputs.tool_args = {}
    ctx.inputs.tool_result = {"content": "ok"}
    await rail.after_tool_call(ctx)
    ctx.inputs.tool_args = {"content": "Network/Cookies"}
    await rail.after_tool_call(ctx)
    ctx.request_force_finish.assert_not_called()

    ctx.inputs.tool_args = {"content": "Login Data"}
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()
    payload = ctx.request_force_finish.call_args[0][0]
    assert payload["result_type"] == "answer"
    assert "凭证" in payload["output"]

@pytest.mark.asyncio
async def test_non_relevant_tool_does_not_trip() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=3, probe_threshold=99)
    ctx = _ctx({"content": "HTTP 401 AUTH_SESSION_TERMINATED"}, tool_name="web_search")
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_generic_authentication_required_is_not_a_strong_fingerprint() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=2, probe_threshold=99)
    ctx = _ctx({"content": "Authentication required"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_not_called()


@pytest.mark.asyncio
async def test_set_language_switches_to_english() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    ctx = _ctx({"content": "AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)
    rail.set_language("en")

    await rail.after_tool_call(ctx)

    payload = ctx.request_force_finish.call_args[0][0]
    assert "sign in" in payload["output"]


@pytest.mark.asyncio
async def test_scans_tail_of_large_tool_result() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    ctx = _ctx({"content": "x" * (128 * 1024) + " AUTH_SESSION_TERMINATED"})
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


@pytest.mark.asyncio
async def test_exec_tool_participates() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    ctx = _ctx({"content": "AUTH_SESSION_TERMINATED"}, tool_name="exec_command")
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


class _ToolOutputLike:
    success = True
    error = None

    def __init__(self, data: str) -> None:
        self.data = data

    def __repr__(self) -> str:
        raise AssertionError("repr must not be called for object tool output")


@pytest.mark.asyncio
async def test_object_tool_output_uses_fields_not_repr() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=1, probe_threshold=99)
    payload = "x" * (4 * 1024 * 1024) + " AUTH_SESSION_TERMINATED"
    ctx = _ctx(_ToolOutputLike(payload))
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()


@pytest.mark.asyncio
async def test_tool_call_arguments_fallback() -> None:
    rail = StalledAccessCircuitRail(auth_threshold=99, probe_threshold=1)
    ctx = SimpleNamespace(
        session=SimpleNamespace(get_session_id=lambda: "session-1"),
        inputs=SimpleNamespace(
            tool_name="bash",
            tool_args=None,
            tool_result={"content": "ok"},
            tool_call=SimpleNamespace(arguments={"content": "CryptUnprotectData"}),
        ),
        request_force_finish=MagicMock(),
    )
    await rail.before_invoke(ctx)

    await rail.after_tool_call(ctx)

    ctx.request_force_finish.assert_called_once()
