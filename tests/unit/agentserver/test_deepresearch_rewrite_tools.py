import hashlib
import inspect
import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest
from openjiuwen.core.runner.callback import get_callback_framework
from openjiuwen.core.runner.callback.events import ToolCallEvents

from jiuwenclaw.agentserver.tools import deepresearch_tools as dt
from jiuwenclaw.agentserver.tools.deepresearch import rewrite_tools as rt


_BASE_REVISION = {
    "document_id": "doc_test",
    "revision_id": "rev_parent",
    "content_sha256": "a" * 64,
}


def _document(root: Path, citation_artifacts: dict[str, str] | None = None):
    body = "原句。\n"
    report = root / "report.md"
    report.write_text(body, encoding="utf-8")
    snapshot = {
        "response_content": body,
        "citation_messages": {"code": 0, "msg": "success", "data": []},
        "infer_messages": [],
        "chart_messages": [],
    }
    snapshot_bytes = json.dumps(
        snapshot, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    snapshot_path = report.with_suffix(".final-result.json")
    snapshot_path.write_bytes(snapshot_bytes)
    provenance = {
        "schema_version": 2,
        "document_id": "doc_test",
        "revision_id": "rev_parent",
        "parent_revision_id": None,
        "conversation_id": "C1",
        "markdown_path": str(report),
        "content_sha256": hashlib.sha256(body.encode()).hexdigest(),
        "final_result_path": snapshot_path.name,
        "final_result_sha256": hashlib.sha256(snapshot_bytes).hexdigest(),
        "created_at": "2026-07-15T00:00:00+00:00",
        "operation": {"action": "deepresearch_generate"},
        "citations": [],
        "inference_manifest": [],
        "chart_manifest": [],
        "rewrite_history": [],
    }
    if citation_artifacts is not None:
        provenance["citation_artifacts"] = citation_artifacts
    report.with_suffix(".provenance.json").write_text(json.dumps(provenance), encoding="utf-8")
    return report, provenance


def _selection() -> dict:
    selected = "原句。"
    selected_bytes = selected.encode("utf-8")
    return {
        "protocol_version": 2,
        "start_byte": 0,
        "end_byte": len(selected_bytes),
        "selected_text": selected,
        "source_sha256": hashlib.sha256(selected_bytes).hexdigest(),
    }


def _structured_result(prepared: dict, text: str = "新句。") -> dict:
    prepared_unit = prepared["units"][0]
    prepared_slot = prepared_unit["slots"][0]
    return {
        "units": [
            {
                "unit_id": prepared_unit["unit_id"],
                "slots": [{"slot_id": prepared_slot["slot_id"], "text": text}],
            }
        ],
        "facts_added": False,
    }


_INVOKE_LIFECYCLE_EVENTS = (
    ToolCallEvents.TOOL_INVOKE_INPUT,
    ToolCallEvents.TOOL_CALL_STARTED,
    ToolCallEvents.TOOL_PARSE_STARTED,
    ToolCallEvents.TOOL_PARSE_FINISHED,
    ToolCallEvents.TOOL_CALL_FINISHED,
    ToolCallEvents.TOOL_INVOKE_OUTPUT,
)
_INVOKE_AUDIT_EVENTS = _INVOKE_LIFECYCLE_EVENTS + (
    ToolCallEvents.TOOL_CALL_ERROR,
)


async def _invoke_with_event_probe(tool_instance, payload):
    framework = get_callback_framework()
    observed = []
    callbacks = []
    for event in _INVOKE_AUDIT_EVENTS:
        async def capture(*_args, _event=event, **_kwargs):
            observed.append(_event)

        framework.register_sync(event, capture, namespace="rewrite-tool-contract-test")
        callbacks.append((event, capture))
    try:
        try:
            result = await tool_instance.invoke(payload)
        except Exception as exc:  # the probe asserts whether errors escape
            result = exc
    finally:
        for event, callback in callbacks:
            await framework.unregister(event, callback)
    return result, observed


def test_rewrite_tool_schemas_expose_only_protocol_v2_contract():
    assert list(inspect.signature(rt.deepresearch_prepare_rewrite._func).parameters) == [
        "report_path",
        "action",
        "selection",
        "instruction",
        "base_revision",
    ]
    assert list(inspect.signature(rt.deepresearch_commit_rewrite._func).parameters) == [
        "context_token",
        "structured_result",
    ]
    prepare_card = rt.deepresearch_prepare_rewrite._card
    assert list(prepare_card.input_params["properties"]) == [
        "report_path",
        "action",
        "selection",
        "instruction",
        "base_revision",
    ]
    assert "Protocol v2" in prepare_card.description
    assert "UTF-8" in prepare_card.description
    assert "byte" in prepare_card.description
    prepare_schema = prepare_card.input_params
    assert prepare_schema["required"] == ["report_path", "action", "selection"]
    assert prepare_schema["additionalProperties"] is False
    assert prepare_schema["properties"]["action"]["enum"] == [
        "polish",
        "expand",
        "shorten",
    ]
    selection_schema = prepare_schema["properties"]["selection"]
    assert selection_schema["required"] == [
        "protocol_version",
        "start_byte",
        "end_byte",
        "selected_text",
        "source_sha256",
    ]
    assert selection_schema["additionalProperties"] is False
    assert selection_schema["properties"]["protocol_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert selection_schema["properties"]["start_byte"] == {
        "type": "integer",
        "minimum": 0,
    }
    assert selection_schema["properties"]["end_byte"] == {
        "type": "integer",
        "minimum": 0,
    }
    assert selection_schema["properties"]["source_sha256"] == {
        "type": "string",
        "pattern": "^[0-9a-f]{64}$",
    }
    assert prepare_schema["properties"]["base_revision"] == {
        "type": "object",
        "properties": {
            "document_id": {
                "type": "string",
                "pattern": "^doc_[A-Za-z0-9_-]{1,128}$",
            },
            "revision_id": {
                "type": "string",
                "pattern": "^rev_[A-Za-z0-9_-]{1,128}$",
            },
            "content_sha256": {
                "type": "string",
                "pattern": "^[0-9a-f]{64}$",
            },
        },
        "required": ["document_id", "revision_id", "content_sha256"],
        "additionalProperties": False,
    }
    commit_card = rt.deepresearch_commit_rewrite._card
    assert list(commit_card.input_params["properties"]) == [
        "context_token",
        "structured_result",
    ]
    assert "units" in commit_card.description
    commit_schema = commit_card.input_params
    assert commit_schema["required"] == ["context_token", "structured_result"]
    assert commit_schema["additionalProperties"] is False
    structured_schema = commit_schema["properties"]["structured_result"]
    assert structured_schema["required"] == ["units", "facts_added"]
    assert structured_schema["additionalProperties"] is False
    assert structured_schema["properties"]["facts_added"] == {
        "type": "boolean",
        "const": False,
    }
    unit_schema = structured_schema["properties"]["units"]["items"]
    assert unit_schema["required"] == ["unit_id", "slots"]
    assert unit_schema["additionalProperties"] is False
    slot_schema = unit_schema["properties"]["slots"]["items"]
    assert slot_schema["required"] == ["slot_id", "text"]
    assert slot_schema["additionalProperties"] is False

    html_tool = rt.deepresearch_generate_rewrite_html
    assert list(inspect.signature(html_tool._func).parameters) == [
        "report_path",
        "revision_id",
    ]
    html_card = html_tool._card
    assert html_card.name == "deepresearch_generate_rewrite_html"
    assert (
        "latest successful deepresearch_commit_rewrite result"
        in html_card.description
    )
    assert html_card.input_params == {
        "type": "object",
        "properties": {
            "report_path": {"type": "string"},
            "revision_id": {
                "type": "string",
                "pattern": "^rev_[A-Za-z0-9_-]{1,128}$",
            },
        },
        "required": ["report_path", "revision_id"],
        "additionalProperties": False,
    }


@pytest.mark.asyncio
async def test_prepare_invoke_keeps_full_lifecycle_for_valid_and_invalid_inputs():
    valid_payload = {
        "report_path": "/workspace/report.md",
        "action": "polish",
        "selection": _selection(),
    }
    invalid_payload = {
        **valid_payload,
        "selection": {**_selection(), "extra": "sensitive extra"},
    }
    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt, "prepare_rewrite", return_value={"context_token": "T1", "units": []}
    ) as core:
        valid_raw, valid_events = await _invoke_with_event_probe(
            rt.deepresearch_prepare_rewrite, valid_payload
        )
        invalid_raw, invalid_events = await _invoke_with_event_probe(
            rt.deepresearch_prepare_rewrite, invalid_payload
        )

    assert json.loads(valid_raw)["status"] == "prepared"
    assert json.loads(invalid_raw)["error_code"] == "BAD_REQUEST"
    assert "sensitive" not in invalid_raw
    assert core.call_count == 1
    assert set(valid_events) == set(_INVOKE_LIFECYCLE_EVENTS)
    assert invalid_events == valid_events


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("tool_instance", "payload", "core_name", "error_code"),
    [
        (
            rt.deepresearch_prepare_rewrite,
            {
                "report_path": "/SECRET/report.md",
                "action": "polish",
                "selection": _selection(),
                "extra": "SECRET-extra",
            },
            "prepare_rewrite",
            "BAD_REQUEST",
        ),
        (
            rt.deepresearch_prepare_rewrite,
            {"report_path": "/SECRET/report.md", "action": "polish"},
            "prepare_rewrite",
            "BAD_REQUEST",
        ),
        (
            rt.deepresearch_commit_rewrite,
            {
                "context_token": "token",
                "structured_result": {
                    "units": [{
                        "unit_id": "unit_1",
                        "slots": [{"slot_id": "slot_1", "text": "new"}],
                    }],
                    "facts_added": False,
                },
                "extra": "SECRET-extra",
            },
            "commit_rewrite",
            "MODEL_OUTPUT_INVALID",
        ),
        (
            rt.deepresearch_commit_rewrite,
            {"context_token": "SECRET-token"},
            "commit_rewrite",
            "MODEL_OUTPUT_INVALID",
        ),
    ],
)
async def test_top_level_parse_errors_return_safe_json_inside_lifecycle(
    tool_instance, payload, core_name, error_code
):
    with patch.object(rt, core_name, side_effect=AssertionError("core called")) as core:
        result, events = await _invoke_with_event_probe(tool_instance, payload)

    assert isinstance(result, str)
    assert json.loads(result) == {
        "status": "error",
        "error_code": error_code,
        "error": "invalid tool input",
    }
    assert "SECRET" not in result
    assert "/SECRET/report.md" not in result
    core.assert_not_called()
    assert events == [
        ToolCallEvents.TOOL_INVOKE_INPUT,
        ToolCallEvents.TOOL_CALL_STARTED,
        ToolCallEvents.TOOL_PARSE_STARTED,
        ToolCallEvents.TOOL_CALL_FINISHED,
        ToolCallEvents.TOOL_INVOKE_OUTPUT,
    ]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("change", "value", "error_code"),
    [
        ("action", "delete", "BAD_REQUEST"),
        ("selection_extra", "sensitive extra", "BAD_REQUEST"),
        ("protocol_version", 1, "SELECTION_PROTOCOL_UNSUPPORTED"),
        (
            "base_revision",
            {**_BASE_REVISION, "extra": "sensitive extra"},
            "BAD_REQUEST",
        ),
        (
            "base_revision",
            {
                key: value
                for key, value in _BASE_REVISION.items()
                if key != "revision_id"
            },
            "BAD_REQUEST",
        ),
        ("base_revision", "rev_parent", "BAD_REQUEST"),
        (
            "base_revision",
            {**_BASE_REVISION, "content_sha256": "A" * 64},
            "BAD_REQUEST",
        ),
    ],
)
async def test_prepare_invoke_rejects_contract_violations_before_core(
    change, value, error_code
):
    selection = _selection()
    payload = {
        "report_path": "/sensitive/report.md",
        "action": "polish",
        "selection": selection,
    }
    if change == "selection_extra":
        selection["extra"] = value
    elif change == "protocol_version":
        selection["protocol_version"] = value
    else:
        payload[change] = value

    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt, "prepare_rewrite", side_effect=AssertionError("core called")
    ) as core:
        raw = await rt.deepresearch_prepare_rewrite.invoke(payload)

    assert json.loads(raw)["error_code"] == error_code
    assert "原句" not in raw
    assert "/sensitive/report.md" not in raw
    core.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "base_revision",
    [
        {**_BASE_REVISION, "extra": "not allowed"},
        {
            key: value
            for key, value in _BASE_REVISION.items()
            if key != "content_sha256"
        },
        123,
        {**_BASE_REVISION, "content_sha256": "A" * 64},
    ],
)
async def test_prepare_func_rejects_invalid_base_revision_before_core(base_revision):
    with patch.object(
        rt, "prepare_rewrite", side_effect=AssertionError("core called")
    ) as core:
        raw = await rt.deepresearch_prepare_rewrite._func(
            report_path="/workspace/report.md",
            action="polish",
            selection=_selection(),
            base_revision=base_revision,
        )

    assert json.loads(raw)["error_code"] == "BAD_REQUEST"
    core.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["structured_extra", "facts_added"])
async def test_commit_invoke_rejects_contract_violations_before_core(change):
    structured_result = {
        "units": [{
            "unit_id": "unit_1",
            "slots": [{"slot_id": "slot_1", "text": "sensitive output"}],
        }],
        "facts_added": False,
    }
    if change == "structured_extra":
        structured_result["extra"] = "sensitive extra"
    else:
        structured_result["facts_added"] = True

    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1"}
    ), patch.object(
        rt, "commit_rewrite", side_effect=AssertionError("core called")
    ) as core:
        raw = await rt.deepresearch_commit_rewrite.invoke({
            "context_token": "sensitive-token",
            "structured_result": structured_result,
        })

    assert json.loads(raw)["error_code"] == "MODEL_OUTPUT_INVALID"
    assert "sensitive" not in raw
    core.assert_not_called()


@pytest.mark.asyncio
async def test_prepare_and_commit_tools_return_short_outcomes_and_deliver_file(tmp_path):
    report, _ = _document(tmp_path)
    selection = _selection()
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ):
        prepared_raw = await rt.deepresearch_prepare_rewrite._func(
            report_path=str(report),
            action="polish",
            selection=selection,
            instruction="",
        )
        prepared = json.loads(prepared_raw)
        push = AsyncMock()
        with patch(
            "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
            return_value=push,
        ):
            committed_raw = await rt.deepresearch_commit_rewrite._func(
                context_token=prepared["context_token"],
                structured_result=_structured_result(prepared),
            )

    committed = json.loads(committed_raw)
    assert prepared["status"] == "prepared"
    assert committed["status"] == "completed"
    assert committed["report_delivered"] is True
    assert committed["delivery_status"] == "delivered"
    assert committed["delivery_error_code"] is None
    assert committed["citation_integrity_status"] == "verified"
    assert committed["citation_semantic_status"] == "not_verified"
    assert "citation_status" not in committed
    assert Path(committed["report_path"]).is_file()
    push.send_push.assert_awaited_once()
    assert push.send_push.await_args.args[0]["payload"]["event_type"] == "chat.file"


@pytest.mark.asyncio
async def test_commit_inherits_and_delivers_hidden_citation_artifacts(tmp_path):
    citation_artifacts = {
        "raw_report_path": str(tmp_path / "report.raw_report.md"),
        "citations_preview_path": str(tmp_path / "report.citations.preview.json"),
    }
    report, _ = _document(tmp_path, citation_artifacts=citation_artifacts)
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ):
        prepared = json.loads(
            await rt.deepresearch_prepare_rewrite._func(
                report_path=str(report),
                action="polish",
                selection=_selection(),
                instruction="",
            )
        )
        push = AsyncMock()
        with patch(
            "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
            return_value=push,
        ):
            committed = json.loads(
                await rt.deepresearch_commit_rewrite._func(
                    context_token=prepared["context_token"],
                    structured_result=_structured_result(prepared),
                )
            )

    child_provenance = json.loads(
        Path(committed["provenance_path"]).read_text(encoding="utf-8")
    )
    assert child_provenance["citation_artifacts"] == citation_artifacts
    payload = push.send_push.await_args.args[0]["payload"]
    assert payload["files"] == [{
        "path": committed["report_path"],
        "name": Path(committed["report_path"]).name,
    }]
    assert payload["metadata"] == {
        "artifactBundle": {
            "schemaVersion": "1.0",
            "relatedArtifacts": [
                {
                    "type": "raw_report",
                    "path": citation_artifacts["raw_report_path"],
                    "contentType": "text/markdown",
                    "relatedToPathIndex": 0,
                },
                {
                    "type": "citations_preview",
                    "path": citation_artifacts["citations_preview_path"],
                    "contentType": "application/json",
                    "schemaVersion": "1.1",
                    "relatedToPathIndex": 0,
                },
            ],
        }
    }
    assert "citations_path" not in json.dumps(payload)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "provenance_content",
    [None, "not-json", "[]", '{"citation_artifacts": "invalid"}'],
)
async def test_deliver_report_omits_metadata_for_unusable_provenance(
    tmp_path, provenance_content
):
    provenance_path = tmp_path / "child.provenance.json"
    if provenance_content is not None:
        provenance_path.write_text(provenance_content, encoding="utf-8")
    push = AsyncMock()
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    with patch(
        "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
        return_value=push,
    ):
        delivered = await rt._deliver_report(
            str(tmp_path / "child.md"), str(provenance_path), route
        )

    assert delivered is True
    payload = push.send_push.await_args.args[0]["payload"]
    assert payload["event_type"] == "chat.file"
    assert "metadata" not in payload


@pytest.mark.asyncio
async def test_deliver_report_omits_metadata_for_oversized_provenance(tmp_path):
    provenance_path = tmp_path / "child.provenance.json"
    provenance_path.write_text(
        json.dumps({"citation_artifacts": {"raw_report_path": "x" * 100}}),
        encoding="utf-8",
    )
    push = AsyncMock()
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    with patch.object(rt, "_CITATION_PROVENANCE_MAX_BYTES", 32), patch(
        "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
        return_value=push,
    ):
        delivered = await rt._deliver_report(
            str(tmp_path / "child.md"), str(provenance_path), route
        )

    assert delivered is True
    assert "metadata" not in push.send_push.await_args.args[0]["payload"]


@pytest.mark.asyncio
async def test_prepare_core_exception_returns_safe_internal_error(tmp_path, caplog):
    report, _ = _document(tmp_path)
    route = {"session_id": "S1"}
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ), patch.object(
        rt, "prepare_rewrite", side_effect=RuntimeError("secret /internal/path")
    ), caplog.at_level(logging.ERROR, logger=rt.__name__):
        raw = await rt.deepresearch_prepare_rewrite._func(
            report_path=str(report), action="polish", selection=_selection()
        )

    assert json.loads(raw) == {
        "status": "error",
        "error_code": "INTERNAL_ERROR",
        "error": "rewrite preparation failed",
    }
    assert "secret" not in raw
    assert "/internal/path" not in raw
    assert "secret" not in caplog.text
    assert "/internal/path" not in caplog.text


@pytest.mark.asyncio
async def test_commit_core_exception_returns_safe_write_error_before_delivery(caplog):
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    delivery = AsyncMock()
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "commit_rewrite", side_effect=OSError("secret /internal/path")
    ), patch.object(rt, "_deliver_report", delivery), caplog.at_level(
        logging.ERROR, logger=rt.__name__
    ):
        raw = await rt.deepresearch_commit_rewrite._func(
            context_token="token",
            structured_result={
                "units": [{
                    "unit_id": "unit_1",
                    "slots": [{"slot_id": "slot_1", "text": "new"}],
                }],
                "facts_added": False,
            },
        )

    assert json.loads(raw) == {
        "status": "error",
        "error_code": "WRITE_FAILED",
        "error": "rewrite commit failed",
    }
    assert "secret" not in raw
    assert "/internal/path" not in raw
    assert "secret" not in caplog.text
    assert "/internal/path" not in caplog.text
    delivery.assert_not_awaited()


@pytest.mark.asyncio
async def test_commit_without_channel_keeps_completed_revision_and_marks_delivery_failed(tmp_path):
    report, _ = _document(tmp_path)
    route = {"request_id": "R1", "session_id": "S1"}
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ):
        prepared = json.loads(
            await rt.deepresearch_prepare_rewrite._func(
                report_path=str(report), action="polish", selection=_selection()
            )
        )
        committed = json.loads(
            await rt.deepresearch_commit_rewrite._func(
                context_token=prepared["context_token"],
                structured_result=_structured_result(prepared),
            )
        )
        repeated = json.loads(
            await rt.deepresearch_commit_rewrite._func(
                context_token=prepared["context_token"],
                structured_result=_structured_result(prepared),
            )
        )

    assert committed["status"] == "completed"
    assert committed["report_delivered"] is False
    assert committed["delivery_status"] == "failed"
    assert committed["delivery_error_code"] == "REPORT_DELIVERY_FAILED"
    assert "error_code" not in committed
    assert Path(committed["report_path"]).is_file()
    assert Path(committed["provenance_path"]).is_file()
    assert committed["document_id"] == "doc_test"
    assert committed["revision_id"].startswith("rev_")
    assert committed["citation_integrity_status"] == "verified"
    assert committed["citation_semantic_status"] == "not_verified"
    assert repeated["error_code"] == "CONTEXT_EXPIRED"


@pytest.mark.asyncio
async def test_commit_transport_failure_does_not_mask_completed_revision(tmp_path):
    report, _ = _document(tmp_path)
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ):
        prepared = json.loads(
            await rt.deepresearch_prepare_rewrite._func(
                report_path=str(report), action="polish", selection=_selection()
            )
        )
        push = AsyncMock()
        push.send_push.side_effect = RuntimeError("secret transport detail")
        with patch(
            "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
            return_value=push,
        ):
            committed_raw = await rt.deepresearch_commit_rewrite._func(
                context_token=prepared["context_token"],
                structured_result=_structured_result(prepared),
            )

    committed = json.loads(committed_raw)
    assert committed["status"] == "completed"
    assert committed["report_delivered"] is False
    assert committed["delivery_status"] == "failed"
    assert committed["delivery_error_code"] == "REPORT_DELIVERY_FAILED"
    assert "error_code" not in committed
    assert "secret transport detail" not in committed_raw
    assert Path(committed["report_path"]).is_file()


@pytest.mark.asyncio
@pytest.mark.parametrize("base_revision", [_BASE_REVISION, None])
async def test_prepare_passes_protocol_v2_contract_to_core_unchanged(
    tmp_path, base_revision
):
    report, _ = _document(tmp_path)
    selection = _selection()
    prepared = {"context_token": "T1", "units": []}
    with patch.object(rt, "_get_route", return_value={"session_id": "S1"}), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ), patch.object(rt, "prepare_rewrite", return_value=prepared) as core_prepare:
        raw = await rt.deepresearch_prepare_rewrite._func(
            report_path=str(report),
            action="polish",
            selection=selection,
            base_revision=base_revision,
        )

    assert json.loads(raw) == {"status": "prepared", **prepared}
    assert core_prepare.call_args.kwargs["selection"] is selection
    assert core_prepare.call_args.kwargs.get("base_revision") is base_revision


@pytest.mark.asyncio
async def test_prepare_tool_returns_stable_error_without_leaking_selection(tmp_path, caplog):
    report, _ = _document(tmp_path)
    report.write_text("已变化。\n", encoding="utf-8")
    selection = _selection()
    with patch.object(rt, "_get_route", return_value={"session_id": "S1"}), patch.object(
        rt, "get_effective_request_output_dir", return_value=str(tmp_path)
    ), caplog.at_level(logging.INFO, logger=rt.__name__):
        raw = await rt.deepresearch_prepare_rewrite._func(
            report_path=str(report), action="polish", selection=selection
        )
    result = json.loads(raw)
    assert result == {"status": "error", "error_code": "REVISION_CONFLICT", "error": "the report revision changed"}
    assert "原句" not in raw
    assert "原句" not in caplog.text


def test_deepresearch_catalog_includes_stream_and_rewrite_tools(monkeypatch):
    monkeypatch.setattr(dt, "enable_deepresearch", lambda: True)
    monkeypatch.setattr(dt, "_deepresearch_dependency_available", lambda: True)
    assert dt.get_deepresearch_tools() == [
        dt.deepresearch_stream,
        rt.deepresearch_prepare_rewrite,
        rt.deepresearch_commit_rewrite,
        rt.deepresearch_generate_rewrite_html,
    ]


@pytest.mark.asyncio
async def test_generate_rewrite_html_invoke_keeps_lifecycle_and_rejects_invalid_schema():
    valid_payload = {
        "report_path": "/workspace/rewrite.md",
        "revision_id": "rev_child",
    }
    invalid_payload = {**valid_payload, "extra": "SECRET-extra"}
    export = {
        "report_path": "/workspace/rewrite.md",
        "final_result": {"response_content": "rewritten"},
    }
    html_path = Path("/workspace/rewrite.html")
    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1", "channel_id": "CH1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt, "prepare_html_export", return_value=export
    ) as prepare, patch.object(
        rt, "_generate_report_html", AsyncMock(return_value=html_path)
    ), patch.object(
        rt, "_deliver_html", AsyncMock(return_value=True)
    ):
        valid_raw, valid_events = await _invoke_with_event_probe(
            rt.deepresearch_generate_rewrite_html, valid_payload
        )
        invalid_raw, invalid_events = await _invoke_with_event_probe(
            rt.deepresearch_generate_rewrite_html, invalid_payload
        )

    assert json.loads(valid_raw) == {
        "status": "completed",
        "html_delivered": True,
        "delivery_status": "delivered",
    }
    assert json.loads(invalid_raw) == {
        "status": "error",
        "error_code": "BAD_REQUEST",
        "error": "invalid tool input",
    }
    assert "SECRET" not in invalid_raw
    prepare.assert_called_once()
    assert set(valid_events) == set(_INVOKE_LIFECYCLE_EVENTS)
    assert invalid_events == [
        ToolCallEvents.TOOL_INVOKE_INPUT,
        ToolCallEvents.TOOL_CALL_STARTED,
        ToolCallEvents.TOOL_PARSE_STARTED,
        ToolCallEvents.TOOL_CALL_FINISHED,
        ToolCallEvents.TOOL_INVOKE_OUTPUT,
    ]


@pytest.mark.asyncio
async def test_generate_rewrite_html_passes_inputs_generates_once_and_delivers_only_html():
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    final_result = {"response_content": "rewritten"}
    export = {
        "report_path": "/workspace/child.md",
        "final_result": final_result,
    }
    html_path = Path("/workspace/child.html")
    generator = AsyncMock(return_value=html_path)
    delivery = AsyncMock(return_value=True)
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt, "prepare_html_export", return_value=export
    ) as prepare, patch.object(
        rt, "_generate_report_html", generator
    ), patch.object(
        rt, "_deliver_html", delivery
    ):
        raw = await rt.deepresearch_generate_rewrite_html._func(
            report_path="/workspace/child.md",
            revision_id="rev_child",
        )

    assert json.loads(raw) == {
        "status": "completed",
        "html_delivered": True,
        "delivery_status": "delivered",
    }
    prepare.assert_called_once_with(
        workspace_root="/workspace",
        report_path="/workspace/child.md",
        revision_id="rev_child",
    )
    generator.assert_awaited_once_with(
        final_result,
        Path("/workspace/child.md"),
        "rewritten",
    )
    delivery.assert_awaited_once_with(html_path, route)
    assert "/workspace" not in raw


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("output_dir", "route"),
    [
        (None, {"session_id": "S1", "channel_id": "CH1"}),
        ("/workspace", {"channel_id": "CH1"}),
        ("/workspace", {"session_id": "S1"}),
    ],
)
async def test_generate_rewrite_html_requires_workspace_and_full_route_before_core(
    output_dir, route
):
    with patch.object(rt, "_get_route", return_value=route), patch.object(
        rt, "get_effective_request_output_dir", return_value=output_dir
    ), patch.object(
        rt, "prepare_html_export", side_effect=AssertionError("core called")
    ) as prepare:
        raw = await rt.deepresearch_generate_rewrite_html._func(
            report_path="/SECRET/child.md",
            revision_id="rev_child",
        )

    assert json.loads(raw) == {
        "status": "error",
        "error_code": "BAD_REQUEST",
        "error": "rewrite HTML workspace or route is unavailable",
    }
    assert "SECRET" not in raw
    prepare.assert_not_called()


@pytest.mark.asyncio
@pytest.mark.parametrize("error_code", ["BAD_REQUEST", "REVISION_CONFLICT"])
async def test_generate_rewrite_html_returns_safe_core_errors(error_code, caplog):
    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1", "channel_id": "CH1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt,
        "prepare_html_export",
        side_effect=rt.RewriteError(error_code, "SECRET /internal/source.md"),
    ), caplog.at_level(logging.INFO, logger=rt.__name__):
        raw = await rt.deepresearch_generate_rewrite_html._func(
            report_path="/SECRET/child.md",
            revision_id="rev_child",
        )

    expected_message = {
        "BAD_REQUEST": "invalid HTML export request",
        "REVISION_CONFLICT": "rewrite revision is unavailable",
    }[error_code]
    assert json.loads(raw) == {
        "status": "error",
        "error_code": error_code,
        "error": expected_message,
    }
    assert "SECRET" not in raw
    assert "/internal" not in raw
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "generation_effect",
    [None, RuntimeError("SECRET /internal/generated.html")],
)
async def test_generate_rewrite_html_generation_failure_does_not_deliver(
    generation_effect, caplog
):
    generator = AsyncMock()
    if isinstance(generation_effect, Exception):
        generator.side_effect = generation_effect
    else:
        generator.return_value = generation_effect
    delivery = AsyncMock()
    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1", "channel_id": "CH1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt,
        "prepare_html_export",
        return_value={
            "report_path": "/workspace/child.md",
            "final_result": {"response_content": "rewritten"},
        },
    ), patch.object(
        rt, "_generate_report_html", generator
    ), patch.object(
        rt, "_deliver_html", delivery
    ), caplog.at_level(logging.ERROR, logger=rt.__name__):
        raw = await rt.deepresearch_generate_rewrite_html._func(
            report_path="/workspace/child.md",
            revision_id="rev_child",
        )

    assert json.loads(raw) == {
        "status": "error",
        "error_code": "HTML_GENERATION_FAILED",
        "error": "HTML generation failed; the Markdown rewrite remains available",
    }
    assert "SECRET" not in raw
    assert "/internal" not in raw
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text
    generator.assert_awaited_once()
    delivery.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "delivery_effect",
    [False, RuntimeError("SECRET /internal/delivery")],
)
async def test_generate_rewrite_html_delivery_failure_is_safe(delivery_effect, caplog):
    delivery = AsyncMock()
    if isinstance(delivery_effect, Exception):
        delivery.side_effect = delivery_effect
    else:
        delivery.return_value = delivery_effect
    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1", "channel_id": "CH1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt,
        "prepare_html_export",
        return_value={
            "report_path": "/workspace/child.md",
            "final_result": {"response_content": "rewritten"},
        },
    ), patch.object(
        rt, "_generate_report_html", AsyncMock(return_value=Path("/workspace/child.html"))
    ), patch.object(
        rt, "_deliver_html", delivery
    ), caplog.at_level(logging.ERROR, logger=rt.__name__):
        raw = await rt.deepresearch_generate_rewrite_html._func(
            report_path="/workspace/child.md",
            revision_id="rev_child",
        )

    assert json.loads(raw) == {
        "status": "error",
        "error_code": "HTML_DELIVERY_FAILED",
        "error": "HTML delivery failed; the Markdown rewrite remains available",
    }
    assert "SECRET" not in raw
    assert "/internal" not in raw
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text
    delivery.assert_awaited_once()


@pytest.mark.asyncio
async def test_generate_rewrite_html_unexpected_preparation_failure_is_safe(caplog):
    with patch.object(
        rt, "_get_route", return_value={"session_id": "S1", "channel_id": "CH1"}
    ), patch.object(
        rt, "get_effective_request_output_dir", return_value="/workspace"
    ), patch.object(
        rt,
        "prepare_html_export",
        side_effect=RuntimeError("SECRET /internal/preparation"),
    ), caplog.at_level(logging.ERROR, logger=rt.__name__):
        raw = await rt.deepresearch_generate_rewrite_html._func(
            report_path="/SECRET/child.md",
            revision_id="rev_child",
        )

    assert json.loads(raw) == {
        "status": "error",
        "error_code": "INTERNAL_ERROR",
        "error": "HTML export preparation failed",
    }
    assert "SECRET" not in raw
    assert "/internal" not in raw
    assert "SECRET" not in caplog.text
    assert "/internal" not in caplog.text


@pytest.mark.asyncio
async def test_deliver_html_sends_exactly_one_html_file_without_metadata():
    push = AsyncMock()
    route = {"request_id": "R1", "channel_id": "CH1", "session_id": "S1"}
    with patch(
        "jiuwenclaw.agentserver.gateway_push.transport.WebSocketGatewayPushTransport",
        return_value=push,
    ):
        delivered = await rt._deliver_html(Path("/workspace/child.html"), route)

    assert delivered is True
    push.send_push.assert_awaited_once_with({
        "request_id": "R1",
        "channel_id": "CH1",
        "session_id": "S1",
        "payload": {
            "event_type": "chat.file",
            "files": [{"path": "/workspace/child.html", "name": "child.html"}],
        },
        "is_complete": False,
    })
