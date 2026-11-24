# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Terminal tool-failure handling in ProgressiveToolRail.

Regression for issue #6368: a request-scoped PPT MCP tool returned an inner
error payload (``{"result": '{"error": {...}}'}``) while the rail stamped the
invoke as ``success: True``. The model saw contradictory success/failure
signals and retried the same tool with the same arguments for minutes until
it gave up on its own.

Two guarantees are pinned here:

1. an inner error payload is reported as ``success: False`` with its message;
2. the same tool + same arguments + same error repeated N times ends the
   round instead of relying on the model to notice.
"""

# pylint: disable=protected-access

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest

from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
    ProgressiveToolRail,
)
from jiuwenswarm.agents.harness.common.tools.invoke_tool_tool import (
    InvokeToolInput,
)


def _card(name: str = "open_pptx", tool_id: str = "pptx-mcp.open_pptx"):
    return SimpleNamespace(
        name=name,
        id=tool_id,
        description="d",
        input_params={},
        properties={"resilience": {"timeout_s": None}},
    )


def _inner_error(message: str = "Source document identity changed") -> dict:
    return {"result": json.dumps({"error": {"code": "INTERNAL_ERROR", "message": message}})}


def _fake_ctx(tool_name: str, arguments: dict, tool_result: object):
    class _FakeContext:
        def __init__(self) -> None:
            self.inputs = SimpleNamespace(
                tool_name=tool_name,
                tool_call=SimpleNamespace(
                    name=tool_name,
                    id="call_1",
                    arguments=json.dumps(arguments, ensure_ascii=False),
                ),
                tool_result=tool_result,
            )
            self.extra: dict = {}
            self.force_finish_calls: list = []

        def request_force_finish(self, payload=None) -> None:
            self.force_finish_calls.append(payload)

    return _FakeContext()


def _rail(enabled: bool = True, threshold: int = 3) -> ProgressiveToolRail:
    """Build a rail with the failure-loop policy pinned (no host get_config())."""
    rail = ProgressiveToolRail(eager_tools=["tools_search", "invoke_tool"])
    rail._failure_loop_enabled = enabled
    rail._failure_loop_threshold = threshold
    return rail


def _invoke_arguments(target: str = "open_pptx", **inner) -> dict:
    return {"tool_name": target, "arguments": inner}


@pytest.mark.asyncio
async def test_inner_error_payload_is_reported_as_failure():
    """An MCP result whose text carries {"error": ...} must not be success."""
    card = _card()
    target = SimpleNamespace(invoke=AsyncMock(return_value=_inner_error()))
    rail = _rail()
    rail._cached_deferred_tool_infos = [card]

    with patch.object(
        rail,
        "_resolve_tool_instance_with_recovery",
        new=AsyncMock(return_value=(target, card.id, card.id)),
    ):
        result = await rail._invoke_target_tool(
            None,
            InvokeToolInput(tool_name="open_pptx", arguments={"requestId": "copy_002"}),
        )

    assert result["success"] is False
    assert "Source document identity changed" in result["error"]
    assert "result" in result


@pytest.mark.asyncio
async def test_repeated_identical_tool_failures_force_finish():
    """Same target + args + error three times stops the round with an answer."""
    rail = _rail()
    ctx = _fake_ctx(
        "invoke_tool",
        _invoke_arguments("open_pptx", requestId="copy_002"),
        {"status": "error", "success": False, "error_message": "Source document identity changed"},
    )

    await rail.after_tool_call(ctx)
    await rail.after_tool_call(ctx)
    assert ctx.force_finish_calls == []

    await rail.after_tool_call(ctx)
    assert len(ctx.force_finish_calls) == 1
    payload = ctx.force_finish_calls[0]
    assert payload["result_type"] == "answer"
    assert "open_pptx" in payload["output"]
    assert "Source document identity changed" in payload["output"]


@pytest.mark.asyncio
async def test_false_success_wrapper_still_counts_as_failure():
    """Defensive: even an un-normalized success:True wrapper is unwrapped."""
    rail = _rail()
    ctx = _fake_ctx(
        "invoke_tool",
        _invoke_arguments("switch_file", requestId="switch_001"),
        {"success": True, "tool_name": "switch_file", "result": _inner_error("Source document identity changed")},
    )

    for _ in range(3):
        await rail.after_tool_call(ctx)

    assert len(ctx.force_finish_calls) == 1
    assert "switch_file" in ctx.force_finish_calls[0]["output"]


@pytest.mark.asyncio
async def test_different_errors_do_not_trip():
    """A different error text is new information, not a stuck loop."""
    rail = _rail()
    ctx = _fake_ctx("invoke_tool", _invoke_arguments("open_pptx", requestId="copy_002"), None)

    for message in ("first failure", "second failure", "third failure"):
        ctx.inputs.tool_result = {
            "status": "error",
            "success": False,
            "error_message": message,
        }
        await rail.after_tool_call(ctx)
        assert ctx.force_finish_calls == []


@pytest.mark.asyncio
async def test_success_resets_the_streak():
    """A successful call with the same arguments clears the counter."""
    rail = _rail()
    ctx = _fake_ctx("invoke_tool", _invoke_arguments("open_pptx", requestId="copy_002"), None)
    failure = {"status": "error", "success": False, "error_message": "same error"}
    success = {"status": "success", "success": True, "data": {"ok": True}}

    for result in (failure, failure, success, failure, failure):
        ctx.inputs.tool_result = result
        await rail.after_tool_call(ctx)
        assert ctx.force_finish_calls == []


@pytest.mark.asyncio
async def test_direct_tool_failures_are_counted_by_tool_name():
    """Direct (non invoke_tool) calls use their own name in the key."""
    rail = _rail()
    ctx = _fake_ctx("open_pptx", {"workingPath": "D:/x.pptx", "requestId": "copy_002"}, None)
    ctx.inputs.tool_result = {
        "status": "error",
        "success": False,
        "error_message": "same error",
    }

    for _ in range(3):
        await rail.after_tool_call(ctx)

    assert len(ctx.force_finish_calls) == 1
    assert "open_pptx" in ctx.force_finish_calls[0]["output"]


@pytest.mark.asyncio
async def test_success_payload_is_not_treated_as_failure():
    """A successful MCP result nested under ``result`` must not trip the guard."""
    rail = _rail()
    ctx = _fake_ctx("invoke_tool", _invoke_arguments("open_pptx", requestId="copy_002"), None)
    ctx.inputs.tool_result = {
        "success": True,
        "tool_name": "open_pptx",
        "result": {"result": json.dumps({"status": "ready", "documentId": "doc-1"})},
    }

    for _ in range(5):
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_calls == []


@pytest.mark.asyncio
async def test_disabled_guard_does_not_trip():
    """``enabled: false`` keeps the loop running (explicit opt-out)."""
    rail = _rail(enabled=False)
    ctx = _fake_ctx("invoke_tool", _invoke_arguments("open_pptx", requestId="copy_002"), None)
    ctx.inputs.tool_result = {
        "status": "error",
        "success": False,
        "error_message": "same error",
    }

    for _ in range(5):
        await rail.after_tool_call(ctx)

    assert ctx.force_finish_calls == []


@pytest.mark.asyncio
async def test_errors_sharing_display_prefix_are_distinguished():
    """Errors that differ only beyond 300 chars are still different errors."""
    rail = _rail(threshold=3)
    ctx = _fake_ctx("invoke_tool", _invoke_arguments("open_pptx", requestId="copy_002"), None)
    prefix = "x" * 350

    for message in (prefix + "A", prefix + "B", prefix + "A", prefix + "B", prefix + "A"):
        ctx.inputs.tool_result = {
            "status": "error",
            "success": False,
            "error_message": message,
        }
        await rail.after_tool_call(ctx)
        assert ctx.force_finish_calls == []


@pytest.mark.asyncio
async def test_guard_exception_does_not_skip_deepresearch_release():
    """The observation-only guard must never block tool lifecycle cleanup."""
    from jiuwenswarm.agents.harness.common.rails.progressive_tool_rail import (
        _EAGER_DEEPRESEARCH_BINDINGS_KEY,
    )

    rail = _rail()
    ctx = _fake_ctx(
        "deepresearch_execute",
        {"query": "x"},
        {"status": "success", "success": True},
    )
    released = []

    class _Manager:
        def __exit__(self, *_args):
            released.append(True)
            return False

    ctx.extra[_EAGER_DEEPRESEARCH_BINDINGS_KEY] = {"call_1": _Manager()}

    with patch(
        "jiuwenswarm.agents.harness.common.rails.progressive_tool_rail._unwrap_tool_failure",
        side_effect=RuntimeError("boom"),
    ):
        await rail.after_tool_call(ctx)

    assert released == [True]
    assert ctx.force_finish_calls == []
