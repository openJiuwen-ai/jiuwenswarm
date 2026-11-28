# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Business-state tool failures must not be reported as success.

Regression for the PPT round where ``apply_ops`` returned
``{"applied": false, "status": "rejected", "failures": [...]}`` inside a
successful RPC envelope (``{"result": "<json text>"}``). ``invoke_tool``
stamped ``success: True`` and the stream rail emitted a neutral result, so both
the model and the UI believed the edit had landed.

Three separate predicates are pinned here because they must agree:

1. ``business_failure_signal`` — the shared business-state detector;
2. ``_unwrap_tool_failure`` — model-facing ``invoke_tool`` outcome;
3. ``_infer_tool_result_error`` — the ``chat.tool_result`` frame flag.
"""

# pylint: disable=protected-access

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    _unwrap_tool_failure,
)
from jiuwenswarm.agents.harness.common.rails.stream_event_rail import (
    _infer_tool_result_error,
)
from jiuwenswarm.agents.harness.common.tool_result_schema import (
    business_failure_signal,
    normalize_tool_result,
)
from jiuwenswarm.common.mcp_config import _RemoteMcpCallAdapter

REJECTED = {
    "applied": False,
    "status": "rejected",
    "failures": [
        {
            "index": 0,
            "op": {"op": "setText", "target": {"slide": "s_2"}},
            "error": "target text not found",
        }
    ],
}

MCP_ERROR_TEXT = "MCP error -32602: Input validation error: ..."


def _envelope(payload: object) -> dict:
    """Mirror ``RequestScopedOfficeClawMcpTool.invoke``'s ``{"result": text}``."""
    return {"result": json.dumps(payload, ensure_ascii=False)}


def test_business_failure_signal_detects_rejected_apply() -> None:
    failed, text = business_failure_signal(REJECTED)

    assert failed is True
    assert "target text not found" in text


def test_business_failure_signal_ignores_dry_run_and_success() -> None:
    assert business_failure_signal({"applied": False, "dryRun": True}) == (False, "")
    assert business_failure_signal({"applied": False, "dry_run": "true"}) == (False, "")
    assert business_failure_signal({"applied": True, "status": "applied"}) == (False, "")
    assert business_failure_signal({"status": "forwarded"}) == (False, "")


def test_business_failure_signal_lets_explicit_success_win_at_same_level() -> None:
    """同层显式 success=true 优先（与 has_failure_signal 的 success 优先序一致）。"""
    assert business_failure_signal({"success": True, "applied": False}) == (False, "")
    assert business_failure_signal({"success": True, "errors": ["skipped one row"]}) == (False, "")
    assert business_failure_signal({"success": False, "applied": False})[0] is True


def test_business_failure_signal_reads_nested_json_string() -> None:
    failed, text = business_failure_signal(json.dumps(REJECTED, ensure_ascii=False))

    assert failed is True
    assert "target text not found" in text


def test_normalize_text_recognises_bare_mcp_error() -> None:
    normalized = normalize_tool_result(MCP_ERROR_TEXT)

    assert normalized["status"] == "error"
    assert normalized["error_code"] == "mcp_tool_error"


@pytest.mark.parametrize(
    "payload",
    [
        REJECTED,
        {"applied": False, "status": "rejected"},
        {"status": "denied", "message": "permission denied"},
        {"errors": ["op 1 failed"]},
        {"applied": True, "failures": [{"index": 2, "error": "no such shape"}]},
    ],
)
def test_unwrap_tool_failure_sees_through_mcp_envelope(payload: dict) -> None:
    failed, text = _unwrap_tool_failure(_envelope(payload))

    assert failed is True
    assert text


@pytest.mark.parametrize("key", ["result", "data", "raw_output", "rawOutput"])
def test_unwrap_tool_failure_peels_every_envelope_key(key: str) -> None:
    """下探键必须与 ``_infer_tool_result_error`` 一致，否则 data 信封会漏判。"""
    payload = {"success": True, key: {"applied": False, "status": "rejected"}}

    assert _unwrap_tool_failure(payload)[0] is True
    assert _infer_tool_result_error(payload) is True


MIXED_BATCH = [
    {"applied": True, "status": "applied"},
    {"applied": False, "status": "rejected", "reason": "already exists"},
]

ALL_REJECTED_BATCH = [
    {"applied": False, "status": "rejected"},
    {"applied": False, "status": "denied"},
]


def test_mixed_batch_keeps_partial_success_semantics() -> None:
    """3 成功 1 被拒不等于整体失败（既有 normalize 契约）。"""
    assert business_failure_signal(MIXED_BATCH) == (False, "")
    assert _unwrap_tool_failure(MIXED_BATCH) == (False, "")
    assert _infer_tool_result_error(MIXED_BATCH) is False


def test_all_rejected_batch_is_a_failure_everywhere() -> None:
    failed, text = business_failure_signal(ALL_REJECTED_BATCH)

    assert failed is True
    assert text
    assert _unwrap_tool_failure(ALL_REJECTED_BATCH)[0] is True
    assert _infer_tool_result_error(ALL_REJECTED_BATCH) is True


def test_unwrap_tool_failure_detects_bare_mcp_error_text() -> None:
    failed, text = _unwrap_tool_failure({"result": MCP_ERROR_TEXT})

    assert failed is True
    assert text.startswith("MCP error")


def test_unwrap_tool_failure_reads_is_error_envelope() -> None:
    """agent-core now surfaces MCP ``isError`` as ``{"result": ..., "is_error": true}``."""
    failed, _ = _unwrap_tool_failure({"result": "boom", "is_error": True})

    assert failed is True


@pytest.mark.parametrize(
    "payload",
    [
        {"applied": True, "status": "applied"},
        {"applied": False, "dryRun": True},
        {"ok": True},
        "plain success text",
    ],
)
def test_unwrap_tool_failure_keeps_plain_success(payload: object) -> None:
    assert _unwrap_tool_failure({"result": json.dumps(payload)}) == (False, "")


@pytest.mark.parametrize(
    "payload",
    [
        REJECTED,
        {"applied": False, "status": "rejected"},
        {"status": "blocked", "reason": "target busy"},
    ],
)
def test_stream_rail_marks_nested_business_failure_as_error(payload: dict) -> None:
    """Outer ``success: True`` only says the wrapper call succeeded."""
    frame_result = {"success": True, "result": json.dumps(payload, ensure_ascii=False)}

    assert _infer_tool_result_error(frame_result) is True


def test_stream_rail_marks_nested_mcp_error_text_as_error() -> None:
    assert _infer_tool_result_error({"success": True, "result": MCP_ERROR_TEXT}) is True


def test_stream_rail_keeps_wrapper_success_when_inner_is_clean() -> None:
    assert _infer_tool_result_error({"success": True, "result": {"content": "done"}}) is False


def test_nested_scan_prefers_failure_over_clean_sibling_envelope() -> None:
    """多键载荷里 clean 的 result 不能掩盖被拒的 data（两 rail 同结论）。"""
    frame = {
        "success": True,
        "result": {"applied": True, "status": "applied"},
        "data": {"applied": False, "status": "rejected", "reason": "already exists"},
    }

    assert _infer_tool_result_error(frame) is True
    assert _unwrap_tool_failure(frame)[0] is True


def test_stream_rail_ignores_failure_words_inside_long_tool_body() -> None:
    """``invoke_tool`` 的 ``result`` 常是目标工具正文；正文提到失败字样不算失败。"""
    body = "log line\n" * 60 + "troubleshooting: exit code: 1 means ...\n" + "tail\n" * 60

    assert _infer_tool_result_error({"success": True, "result": body}) is False


def test_stream_rail_does_not_regex_a_parsed_json_body() -> None:
    """JSON 文本解析成功后直接返回结构化结论，不再对原文套全文正则。

    否则 ``{"content": "... exit code: 1 ..."}`` 这类成功正文会被误标失败，
    而同键同载荷的 ``_unwrap_tool_failure`` 判成功——判定面分裂。
    """
    body = json.dumps({"content": "doc says: run with exit code: 1 only in debug mode"})
    frame = {"success": True, "result": body}

    assert _infer_tool_result_error(frame) is False
    assert _unwrap_tool_failure(frame) == (False, "")


def test_stream_rail_still_scans_short_nested_failure_text() -> None:
    assert _infer_tool_result_error({"success": True, "result": "Error: no slide 2"}) is True
    assert _infer_tool_result_error({"success": True, "result": "success: False, aborted"}) is True


def test_stream_rail_still_reports_unknown_for_plain_payload() -> None:
    assert _infer_tool_result_error({"content": "done"}) is None


@pytest.mark.asyncio
async def test_remote_adapter_preserves_is_error() -> None:
    class _Client:
        async def call_tool_with_meta(self, name: str, arguments: dict):
            assert name == "apply_ops"
            return MCP_ERROR_TEXT, True

    result = await _RemoteMcpCallAdapter(_Client()).call_tool("apply_ops", {})

    assert result.isError is True
    assert result.content[0].text == MCP_ERROR_TEXT


@pytest.mark.asyncio
async def test_remote_adapter_defaults_to_success_for_legacy_clients() -> None:
    class _LegacyClient:
        async def call_tool(self, name: str, arguments: dict):
            return "ok"

    result = await _RemoteMcpCallAdapter(_LegacyClient()).call_tool("open_pptx", {})

    assert result.isError is False
    assert result.content[0].text == "ok"


@pytest.mark.asyncio
async def test_remote_adapter_ignores_duck_typed_meta_attribute() -> None:
    """``getattr`` 不能把动态生成的 meta 当成真实实现。

    ``MagicMock`` / ``__getattr__`` 代理会凭空响应 ``call_tool_with_meta``；
    一旦优先调用它，真正的 ``call_tool``（超时 / 取消 / 断连路径）就被绕过。
    """
    inner = MagicMock(_owner_task=None)
    inner.call_tool = AsyncMock(return_value="ok")

    result = await _RemoteMcpCallAdapter(inner).call_tool("t", {})

    assert result.isError is False
    assert result.content[0].text == "ok"
    inner.call_tool.assert_awaited_once_with("t", {})


@pytest.mark.asyncio
async def test_remote_adapter_does_not_bypass_a_subclass_call_tool() -> None:
    """子类只覆写 ``call_tool`` 时，继承的 meta 版本不能绕过它（与 agent-core 同口径）。"""
    class _BaseClient:
        async def call_tool(self, name: str, arguments: dict):
            return "base"

        async def call_tool_with_meta(self, name: str, arguments: dict):
            return "meta", True

    class _SubClient(_BaseClient):
        def __init__(self) -> None:
            self.calls: list[tuple[str, dict]] = []

        async def call_tool(self, name: str, arguments: dict):
            self.calls.append((name, arguments))
            return "sub"

    client = _SubClient()
    result = await _RemoteMcpCallAdapter(client).call_tool("t", {})

    assert result.isError is False
    assert result.content[0].text == "sub"
    assert client.calls == [("t", {})]


@pytest.mark.asyncio
async def test_remote_adapter_warns_once_per_client_type(caplog: pytest.LogCaptureFixture) -> None:
    """回退要留痕，但不能逐请求刷屏：同一 client 类型只警告一次。"""

    class _LegacyWarnClient:
        async def call_tool(self, name: str, arguments: dict):
            return "ok"

    adapter = _RemoteMcpCallAdapter(_LegacyWarnClient())
    with caplog.at_level("WARNING"):
        await adapter.call_tool("t", {})
        await adapter.call_tool("t", {})

    warnings = [r for r in caplog.records if "paired call_tool_with_meta" in r.getMessage()]
    assert len(warnings) == 1
    assert "_LegacyWarnClient" in warnings[0].getMessage()
