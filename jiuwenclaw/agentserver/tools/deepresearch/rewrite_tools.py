# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Agent-facing tools for Skill-driven DeepResearch report rewrites."""
from __future__ import annotations

import json
import logging
import os
import re
import functools
from pathlib import Path

from openjiuwen.core.common.exception.errors import StatusCode, ValidationError
from openjiuwen.core.foundation.tool import LocalFunction, ToolCard

from jiuwenclaw.agentserver.tools.deepresearch_plugin.document_rewrite import (
    RewriteError,
    commit_rewrite,
    prepare_html_export,
    prepare_rewrite,
)
from jiuwenclaw.agentserver.tools.deepresearch_tools import (
    _build_related_artifact_bundle,
    _generate_report_html,
    _get_route,
)
from jiuwenclaw.agentserver.tools.subagent_executor.context_vars import (
    get_effective_request_output_dir,
)

logger = logging.getLogger(__name__)

_CITATION_PROVENANCE_MAX_BYTES = 4 * 1024 * 1024

_PREPARE_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "report_path": {"type": "string"},
        "action": {"type": "string", "enum": ["polish", "expand", "shorten"]},
        "selection": {
            "type": "object",
            "properties": {
                "protocol_version": {"type": "integer", "const": 2},
                "start_byte": {"type": "integer", "minimum": 0},
                "end_byte": {"type": "integer", "minimum": 0},
                "selected_text": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": 12_000,
                },
                "source_sha256": {
                    "type": "string",
                    "pattern": "^[0-9a-f]{64}$",
                },
            },
            "required": [
                "protocol_version",
                "start_byte",
                "end_byte",
                "selected_text",
                "source_sha256",
            ],
            "additionalProperties": False,
        },
        "instruction": {"type": "string", "maxLength": 2_000, "default": ""},
        "base_revision": {
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
        },
    },
    "required": ["report_path", "action", "selection"],
    "additionalProperties": False,
}

_COMMIT_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "context_token": {"type": "string"},
        "structured_result": {
            "type": "object",
            "properties": {
                "units": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "properties": {
                            "unit_id": {"type": "string"},
                            "slots": {
                                "type": "array",
                                "minItems": 1,
                                "items": {
                                    "type": "object",
                                    "properties": {
                                        "slot_id": {"type": "string"},
                                        "text": {"type": "string"},
                                    },
                                    "required": ["slot_id", "text"],
                                    "additionalProperties": False,
                                },
                            },
                        },
                        "required": ["unit_id", "slots"],
                        "additionalProperties": False,
                    },
                },
                "facts_added": {"type": "boolean", "const": False},
            },
            "required": ["units", "facts_added"],
            "additionalProperties": False,
        },
    },
    "required": ["context_token", "structured_result"],
    "additionalProperties": False,
}

_HTML_INPUT_SCHEMA = {
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


class _SafeInputLocalFunction(LocalFunction):
    def __init__(self, *, card: ToolCard, func, input_error_code: str):
        self._input_error_code = input_error_code
        super().__init__(card=card, func=func)

    async def invoke(self, inputs, **kwargs):
        try:
            return await super().invoke(inputs, **kwargs)
        except ValidationError as exc:
            if exc.status not in {
                StatusCode.SCHEMA_VALIDATE_INVALID,
                StatusCode.SCHEMA_FORMAT_INVALID,
            }:
                raise
            logger.info(
                "deepresearch tool schema input rejected: tool=%s", self.card.name
            )
            return json.dumps({
                "status": "error",
                "error_code": self._input_error_code,
                "error": "invalid tool input",
            })


def _safe_input_tool(
    *, name: str, description: str, input_params: dict, input_error_code: str
):
    def decorator(func):
        @functools.wraps(func)
        async def wrapped(*args, **kwargs):
            return await func(*args, **kwargs)

        return _SafeInputLocalFunction(
            card=ToolCard(
                name=name,
                description=description,
                input_params=input_params,
            ),
            func=wrapped,
            input_error_code=input_error_code,
        )

    return decorator


def _error(exc: RewriteError) -> str:
    return json.dumps(
        {"status": "error", "error_code": exc.code, "error": str(exc)},
        ensure_ascii=False,
    )


def _validate_prepare_contract(
    report_path: object,
    action: object,
    selection: object,
    instruction: object,
    base_revision: object = None,
) -> None:
    def invalid_request_shape() -> bool:
        return (
            len(instruction) > 2_000
            or not isinstance(action, str)
            or action not in {"polish", "expand", "shorten"}
        )

    def invalid_selection_fields() -> bool:
        return (
            type(start) is not int
            or type(end) is not int
            or start < 0
            or end <= start
            or not isinstance(selected_text, str)
            or not selected_text
            or len(selected_text) > 12_000
            or not isinstance(source_sha256, str)
            or re.fullmatch(r"[0-9a-f]{64}", source_sha256) is None
        )

    if not isinstance(report_path, str) or not isinstance(instruction, str):
        raise RewriteError("BAD_REQUEST", "invalid rewrite request")
    if invalid_request_shape():
        raise RewriteError("BAD_REQUEST", "invalid rewrite request")
    if not isinstance(selection, dict):
        raise RewriteError(
            "SELECTION_PROTOCOL_UNSUPPORTED", "selection protocol version 2 is required"
        )
    if type(selection.get("protocol_version")) is not int or selection.get(
        "protocol_version"
    ) != 2:
        raise RewriteError(
            "SELECTION_PROTOCOL_UNSUPPORTED", "selection protocol version 2 is required"
        )
    if set(selection) != {
        "protocol_version",
        "start_byte",
        "end_byte",
        "selected_text",
        "source_sha256",
    }:
        raise RewriteError("BAD_REQUEST", "invalid rewrite selection")
    start = selection["start_byte"]
    end = selection["end_byte"]
    selected_text = selection["selected_text"]
    source_sha256 = selection["source_sha256"]
    if invalid_selection_fields():
        raise RewriteError("BAD_REQUEST", "invalid rewrite selection")
    if base_revision is not None and (
        not isinstance(base_revision, dict)
        or set(base_revision)
        != {"document_id", "revision_id", "content_sha256"}
        or not isinstance(base_revision.get("document_id"), str)
        or re.fullmatch(
            r"doc_[A-Za-z0-9_-]{1,128}", base_revision["document_id"]
        )
        is None
        or not isinstance(base_revision.get("revision_id"), str)
        or re.fullmatch(
            r"rev_[A-Za-z0-9_-]{1,128}", base_revision["revision_id"]
        )
        is None
        or not isinstance(base_revision.get("content_sha256"), str)
        or re.fullmatch(r"[0-9a-f]{64}", base_revision["content_sha256"])
        is None
    ):
        raise RewriteError("BAD_REQUEST", "invalid rewrite request")


def _validate_commit_contract(context_token: object, structured_result: object) -> None:
    def invalid_slot(slot: object) -> bool:
        return (
            not isinstance(slot, dict)
            or set(slot) != {"slot_id", "text"}
            or not isinstance(slot.get("slot_id"), str)
            or not isinstance(slot.get("text"), str)
        )

    if not isinstance(context_token, str) or not isinstance(structured_result, dict):
        raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
    if set(structured_result) != {"units", "facts_added"}:
        raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
    if structured_result.get("facts_added") is not False:
        raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
    units = structured_result.get("units")
    if not isinstance(units, list) or not units:
        raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
    for unit in units:
        if not isinstance(unit, dict) or set(unit) != {"unit_id", "slots"}:
            raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
        if not isinstance(unit.get("unit_id"), str):
            raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
        slots = unit.get("slots")
        if not isinstance(slots, list) or not slots:
            raise RewriteError("MODEL_OUTPUT_INVALID", "invalid structured rewrite result")
        for slot in slots:
            if invalid_slot(slot):
                raise RewriteError(
                    "MODEL_OUTPUT_INVALID", "invalid structured rewrite result"
                )


@_safe_input_tool(
    name="deepresearch_prepare_rewrite",
    description=(
        "准备 DeepResearch Markdown 局部改写。必须在生成任何改写正文前调用；"
        "selection 必须使用 Protocol v2，start_byte/end_byte 是绝对 UTF-8 byte 偏移；"
        "校验报告 revision、选区和引用白名单，返回一次性 context_token。"
    ),
    input_params=_PREPARE_INPUT_SCHEMA,
    input_error_code="BAD_REQUEST",
)
async def deepresearch_prepare_rewrite(
    report_path: str,
    action: str,
    selection: dict,
    instruction: str = "",
    base_revision: dict | None = None,
) -> str:
    try:
        _validate_prepare_contract(
            report_path, action, selection, instruction, base_revision
        )
    except RewriteError as exc:
        logger.info("deepresearch prepare rewrite rejected: code=%s", exc.code)
        return _error(exc)
    route = _get_route()
    output_dir = get_effective_request_output_dir()
    session_id = str(route.get("session_id") or "")
    if not output_dir or not session_id:
        return json.dumps({
            "status": "error",
            "error_code": "BAD_REQUEST",
            "error": "rewrite workspace or session is unavailable",
        })
    try:
        prepare_kwargs = {
            "workspace_root": output_dir,
            "report_path": report_path,
            "action": action,
            "selection": selection,
            "instruction": instruction,
            "session_id": session_id,
        }
        if base_revision is not None:
            prepare_kwargs["base_revision"] = base_revision
        result = prepare_rewrite(**prepare_kwargs)
    except RewriteError as exc:
        logger.info("deepresearch prepare rewrite rejected: code=%s", exc.code)
        return _error(exc)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "deepresearch prepare rewrite failed: type=%s", type(exc).__name__
        )
        return json.dumps({
            "status": "error",
            "error_code": "INTERNAL_ERROR",
            "error": "rewrite preparation failed",
        })
    return json.dumps({"status": "prepared", **result}, ensure_ascii=False)


def _load_citation_artifacts(provenance_path: str) -> object:
    """Best-effort load of hidden citation associations from child provenance."""
    try:
        with Path(provenance_path).open("rb") as stream:
            raw = stream.read(_CITATION_PROVENANCE_MAX_BYTES + 1)
        if len(raw) > _CITATION_PROVENANCE_MAX_BYTES:
            return None
        provenance = json.loads(raw.decode("utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, TypeError):
        return None
    if not isinstance(provenance, dict):
        return None
    return provenance.get("citation_artifacts")


async def _deliver_report(
    report_path: str,
    provenance_path: str,
    route: dict[str, object],
) -> bool:
    if not route.get("session_id") or not route.get("channel_id"):
        return False
    from jiuwenclaw.agentserver.gateway_push.transport import (  # pylint: disable=import-outside-toplevel
        WebSocketGatewayPushTransport,
    )

    transport = WebSocketGatewayPushTransport()
    payload = {
        "event_type": "chat.file",
        "files": [{"path": report_path, "name": os.path.basename(report_path)}],
    }
    artifact_bundle = _build_related_artifact_bundle(
        _load_citation_artifacts(provenance_path), markdown_index=0
    )
    if artifact_bundle is not None:
        payload["metadata"] = {"artifactBundle": artifact_bundle}
    await transport.send_push({
        "request_id": route.get("request_id", ""),
        "channel_id": route["channel_id"],
        "session_id": route["session_id"],
        "payload": payload,
        "is_complete": False,
    })
    return True


async def _deliver_html(
    html_path: Path,
    route: dict[str, object],
) -> bool:
    if not route.get("session_id") or not route.get("channel_id"):
        return False
    from jiuwenclaw.agentserver.gateway_push.transport import (  # pylint: disable=import-outside-toplevel
        WebSocketGatewayPushTransport,
    )

    transport = WebSocketGatewayPushTransport()
    await transport.send_push({
        "request_id": route.get("request_id", ""),
        "channel_id": route["channel_id"],
        "session_id": route["session_id"],
        "payload": {
            "event_type": "chat.file",
            "files": [{"path": str(html_path), "name": html_path.name}],
        },
        "is_complete": False,
    })
    return True


@_safe_input_tool(
    name="deepresearch_commit_rewrite",
    description=(
        "提交 DeepResearch 局部改写结果并创建不可变 child revision。"
        "只能提交 deepresearch_prepare_rewrite 返回的 context_token 和结构化 units 结果，"
        "禁止直接写报告文件。"
    ),
    input_params=_COMMIT_INPUT_SCHEMA,
    input_error_code="MODEL_OUTPUT_INVALID",
)
async def deepresearch_commit_rewrite(
    context_token: str,
    structured_result: dict,
) -> str:
    try:
        _validate_commit_contract(context_token, structured_result)
    except RewriteError as exc:
        logger.info("deepresearch commit rewrite rejected: code=%s", exc.code)
        return _error(exc)
    route = _get_route()
    session_id = str(route.get("session_id") or "")
    if not session_id:
        return json.dumps({
            "status": "error",
            "error_code": "BAD_REQUEST",
            "error": "rewrite session is unavailable",
        })
    try:
        result = commit_rewrite(
            context_token=context_token,
            session_id=session_id,
            structured_result=structured_result,
        )
    except RewriteError as exc:
        logger.info("deepresearch commit rewrite rejected: code=%s", exc.code)
        return _error(exc)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error("deepresearch commit rewrite failed: type=%s", type(exc).__name__)
        return json.dumps({
            "status": "error",
            "error_code": "WRITE_FAILED",
            "error": "rewrite commit failed",
        })

    try:
        delivered = await _deliver_report(
            result["report_path"], result["provenance_path"], route
        )
    except Exception:  # pylint: disable=broad-exception-caught
        logger.exception("deepresearch rewrite artifact delivery failed")
        delivered = False
    return json.dumps(
        {
            "status": "completed",
            "report_delivered": delivered,
            "delivery_status": "delivered" if delivered else "failed",
            "delivery_error_code": None if delivered else "REPORT_DELIVERY_FAILED",
            **result,
        },
        ensure_ascii=False,
    )


@_safe_input_tool(
    name="deepresearch_generate_rewrite_html",
    description=(
        "Generate and deliver HTML for a committed DeepResearch rewrite. "
        "Inputs must be passed unchanged from the latest successful "
        "deepresearch_commit_rewrite result."
    ),
    input_params=_HTML_INPUT_SCHEMA,
    input_error_code="BAD_REQUEST",
)
async def deepresearch_generate_rewrite_html(
    report_path: str,
    revision_id: str,
) -> str:
    route = _get_route()
    output_dir = get_effective_request_output_dir()
    if (
        not output_dir
        or not route.get("session_id")
        or not route.get("channel_id")
    ):
        return json.dumps({
            "status": "error",
            "error_code": "BAD_REQUEST",
            "error": "rewrite HTML workspace or route is unavailable",
        })

    try:
        export = prepare_html_export(
            workspace_root=output_dir,
            report_path=report_path,
            revision_id=revision_id,
        )
    except RewriteError as exc:
        logger.info("deepresearch rewrite HTML export rejected: code=%s", exc.code)
        if exc.code == "BAD_REQUEST":
            message = "invalid HTML export request"
        elif exc.code == "REVISION_CONFLICT":
            message = "rewrite revision is unavailable"
        else:
            return json.dumps({
                "status": "error",
                "error_code": "INTERNAL_ERROR",
                "error": "HTML export preparation failed",
            })
        return json.dumps({
            "status": "error",
            "error_code": exc.code,
            "error": message,
        })
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "deepresearch rewrite HTML preparation failed: type=%s",
            type(exc).__name__,
        )
        return json.dumps({
            "status": "error",
            "error_code": "INTERNAL_ERROR",
            "error": "HTML export preparation failed",
        })

    try:
        html_path = await _generate_report_html(
            export["final_result"],
            Path(export["report_path"]),
            export["final_result"]["response_content"],
        )
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "deepresearch rewrite HTML generation failed: type=%s",
            type(exc).__name__,
        )
        html_path = None
    if html_path is None:
        return json.dumps({
            "status": "error",
            "error_code": "HTML_GENERATION_FAILED",
            "error": (
                "HTML generation failed; the Markdown rewrite remains available"
            ),
        })

    try:
        delivered = await _deliver_html(html_path, route)
    except Exception as exc:  # pylint: disable=broad-exception-caught
        logger.error(
            "deepresearch rewrite HTML delivery failed: type=%s",
            type(exc).__name__,
        )
        delivered = False
    if not delivered:
        return json.dumps({
            "status": "error",
            "error_code": "HTML_DELIVERY_FAILED",
            "error": "HTML delivery failed; the Markdown rewrite remains available",
        })

    return json.dumps({
        "status": "completed",
        "html_delivered": True,
        "delivery_status": "delivered",
    })


__all__ = [
    "deepresearch_prepare_rewrite",
    "deepresearch_commit_rewrite",
    "deepresearch_generate_rewrite_html",
]
