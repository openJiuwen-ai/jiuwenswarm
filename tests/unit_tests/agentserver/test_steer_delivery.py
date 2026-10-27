# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Tool-free final delivery must cover every still-applicable input."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.server.runtime.agent_adapter import steer_delivery
from jiuwenswarm.server.runtime.agent_adapter.steer_delivery import (
    SessionInputDeliveryError, reconcile_steer_delivery,
)


def _plan(*supplements):
    values = supplements or (("input-1", "Also identify the image"),)
    return SimpleNamespace(
        input_request_ids=tuple(item[0] for item in values), original_request_id="original", original_request="What is 99*99?",
        original_reply="99 * 99 = 9801", supplemental_requests=tuple(
            {"request_id": request_id, "text": text} for request_id, text in values
        ), context=object(), session=object(), reasoning_content="private reasoning never copied",
    )


def _response(reply="9801; image text: PROOF-ABC", coverage=None, *, usage=None, content=None, tool_calls=None):
    if coverage is None:
        coverage = [
            {"id": "original", "status": "included", "evidence": "9801"},
            {"id": "input-1", "status": "included", "evidence": "PROOF-ABC"},
        ]
    return SimpleNamespace(
        content=content if content is not None else json.dumps({"reply": reply, "coverage": coverage}),
        usage_metadata=usage, tool_calls=tool_calls, reasoning_content="private model reasoning",
    )


@pytest.mark.asyncio
async def test_reconcile_uses_two_compact_messages_resolved_model_and_no_tools():
    usage = {"input_tokens": 80, "output_tokens": 20}
    model = SimpleNamespace(invoke=AsyncMock(return_value=_response(usage=usage)))
    result = await reconcile_steer_delivery(model, _plan(), "Image text: PROOF-ABC")
    assert result.content == "9801; image text: PROOF-ABC"
    assert result.usage_records == [usage]
    messages = model.invoke.await_args.args[0]
    assert [message.role for message in messages] == ["system", "user"]
    assert model.invoke.await_args.kwargs == {"tools": None}
    payload = json.loads(messages[1].content)
    assert payload["delivery_data"] == {
        "input_request_ids": ["input-1"], "original_request_id": "original", "original_request": "What is 99*99?",
        "original_reply": "99 * 99 = 9801", "supplemental_requests": [
            {"request_id": "input-1", "text": "Also identify the image"},
        ], "latest_draft": "Image text: PROOF-ABC",
    }
    assert "private" not in messages[1].content
    assert "context" not in payload["delivery_data"] and "session" not in payload["delivery_data"]


@pytest.mark.asyncio
async def test_original_can_be_superseded_by_literal_later_human_cancellation():
    cancel = "Cancel the original calculation; only answer 7+8."
    response = _response("15", [
        {"id": "original", "status": "superseded", "evidence": "Cancel the original calculation"},
        {"id": "input-1", "status": "included", "evidence": "15"},
    ])
    result = await reconcile_steer_delivery(SimpleNamespace(invoke=AsyncMock(return_value=response)),
                                            _plan(("input-1", cancel)), "15")
    assert result.content == "15" and "9801" not in result.content


@pytest.mark.asyncio
async def test_coverage_uses_actual_original_request_id_instead_of_a_reserved_alias():
    plan = _plan()
    plan.original_request_id = "request-from-active-round-42"
    response = _response(coverage=[
        {"id": plan.original_request_id, "status": "included", "evidence": "9801"},
        {"id": "input-1", "status": "included", "evidence": "PROOF-ABC"},
    ])
    model = SimpleNamespace(invoke=AsyncMock(return_value=response))
    result = await reconcile_steer_delivery(model, plan, "PROOF-ABC")
    assert result.content == "9801; image text: PROOF-ABC"
    assert json.loads(model.invoke.await_args.args[0][1].content)["delivery_data"]["original_request_id"] == plan.original_request_id


@pytest.mark.asyncio
async def test_later_supplement_can_replace_an_earlier_supplement():
    plan = _plan(("input-1", "Read the first image"), ("input-2", "Cancel reading the first image; use the second."))
    response = _response("9801; second image: NOVA", [
        {"id": "original", "status": "included", "evidence": "9801"},
        {"id": "input-1", "status": "superseded", "evidence": "Cancel reading the first image"},
        {"id": "input-2", "status": "included", "evidence": "NOVA"},
    ])
    result = await reconcile_steer_delivery(SimpleNamespace(invoke=AsyncMock(return_value=response)), plan, "NOVA")
    assert result.content == "9801; second image: NOVA"


@pytest.mark.asyncio
async def test_one_format_retry_preserves_both_real_usage_records_without_reasoning():
    first_usage, second_usage = {"total_tokens": 10}, {"total_tokens": 20}
    model = SimpleNamespace(invoke=AsyncMock(side_effect=[
        _response(content="not JSON", usage=first_usage), _response(usage=second_usage),
    ]))
    result = await reconcile_steer_delivery(model, _plan(), "PROOF-ABC")
    assert result.usage_records == [first_usage, second_usage]
    assert model.invoke.await_count == 2
    retry_messages = model.invoke.await_args_list[1].args[0]
    assert len(retry_messages) == 2
    assert json.loads(retry_messages[1].content)["validation_feedback"] == "invalid_json_content"
    assert "not JSON" not in retry_messages[1].content and "private model reasoning" not in retry_messages[1].content


@pytest.mark.asyncio
@pytest.mark.parametrize("invalid", [
    _response(content="```json\n{}\n```"),
    _response(coverage=[{"id": "input-1", "status": "included", "evidence": "PROOF-ABC"}]),
    _response(coverage=[{"id": "original", "status": "included", "evidence": "missing result"},
                        {"id": "input-1", "status": "included", "evidence": "PROOF-ABC"}]),
    _response(coverage=[{"id": "original", "status": "superseded", "evidence": "user cancelled it"},
                        {"id": "input-1", "status": "included", "evidence": "PROOF-ABC"}]),
    _response(coverage=[{"id": "original", "status": "included", "evidence": "9801"},
                        {"id": "input-1", "status": "superseded", "evidence": "Also identify the image"}]),
    _response(coverage=[{"id": "original", "status": "included", "evidence": "9801"},
                        {"id": "original", "status": "included", "evidence": "9801"}]),
    _response(coverage=[{"id": "original", "status": "included", "evidence": "9801"},
                        {"id": "invented-id", "status": "included", "evidence": "PROOF-ABC"}]),
    _response(reply=" "),
    _response(coverage=[{"id": "original", "status": "included", "evidence": ""},
                        {"id": "input-1", "status": "included", "evidence": "PROOF-ABC"}]),
    _response(content='{"reply":"wrong","reply":"9801","coverage":[]}'),
    _response(content='{"reply":"9801","coverage":[],"reasoning":"do not leak"}'),
    _response(tool_calls=[{"name": "read_file"}]),
])
async def test_invalid_reply_never_silently_falls_back_to_latest_draft(invalid):
    invalid.usage_metadata = {"total_tokens": 7}
    model = SimpleNamespace(invoke=AsyncMock(return_value=invalid))
    with pytest.raises(SessionInputDeliveryError) as captured:
        await reconcile_steer_delivery(model, _plan(), "PROOF-ABC")
    assert captured.value.code == "SESSION_INPUT_DELIVERY_FAILED"
    assert captured.value.usage_records == [{"total_tokens": 7}, {"total_tokens": 7}]
    assert model.invoke.await_count == 2
    assert "PROOF-ABC" not in str(captured.value)


@pytest.mark.asyncio
async def test_timeout_is_bounded_without_format_retry(monkeypatch):
    monkeypatch.setattr(steer_delivery, "_RECONCILIATION_TIMEOUT_SECONDS", 0.01)

    async def hang(*args, **kwargs):
        await asyncio.Event().wait()

    model = SimpleNamespace(invoke=AsyncMock(side_effect=hang))
    with pytest.raises(SessionInputDeliveryError) as captured:
        await reconcile_steer_delivery(model, _plan(), "PROOF-ABC")
    assert captured.value.usage_records == [] and model.invoke.await_count == 1


@pytest.mark.asyncio
async def test_provider_failure_keeps_prior_format_attempt_usage():
    error = RuntimeError("provider private error")
    error.usage_metadata = {"total_tokens": 3}
    model = SimpleNamespace(invoke=AsyncMock(side_effect=[
        _response(content="bad JSON", usage={"total_tokens": 5}), error,
    ]))
    with pytest.raises(SessionInputDeliveryError) as captured:
        await reconcile_steer_delivery(model, _plan(), "PROOF-ABC")
    assert captured.value.usage_records == [{"total_tokens": 5}, {"total_tokens": 3}]
    assert "private" not in str(captured.value)


@pytest.mark.asyncio
async def test_call_cancellation_propagates():
    model = SimpleNamespace(invoke=AsyncMock(side_effect=asyncio.CancelledError))
    with pytest.raises(asyncio.CancelledError):
        await reconcile_steer_delivery(model, _plan(), "PROOF-ABC")


@pytest.mark.asyncio
async def test_invalid_or_agent_plan_is_rejected_before_model_call():
    plan = _plan()
    plan.supplemental_requests[0]["message_origin"] = "cross_session_agent"
    model = SimpleNamespace(invoke=AsyncMock())
    with pytest.raises(SessionInputDeliveryError) as captured:
        await reconcile_steer_delivery(model, plan, "PROOF-ABC")
    assert captured.value.usage_records == []
    model.invoke.assert_not_awaited()
