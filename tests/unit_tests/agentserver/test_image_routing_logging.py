# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The image-attachment log must name the route the request actually took.

Extraction runs before ``_native_image_input_enabled`` is consulted, so a log
line emitted there cannot know whether the attachment ends up in the model
context window, in the image-understanding tool prompt, or nowhere. These pin
the log to the branch that is actually taken.
"""

from __future__ import annotations

import contextlib
import logging

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

_ADAPTER_LOGGER = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"


@contextlib.contextmanager
def _adapter_logs(level=logging.INFO):
    """Collect records from the logger that emits them.

    ``caplog`` attaches to the root logger and ``setup_logger`` sets
    ``propagate = False`` on ``jiuwenswarm``, so these records only reach the
    root on pytest versions that also attach to non-propagating loggers.
    Attaching directly holds on every version.
    """
    logger = logging.getLogger(_ADAPTER_LOGGER)
    records: list[logging.LogRecord] = []

    class _Collect(logging.Handler):
        def emit(self, record):
            records.append(record)

    handler = _Collect(level)
    original = logger.level
    logger.setLevel(level)
    logger.addHandler(handler)
    try:
        yield records
    finally:
        logger.removeHandler(handler)
        logger.setLevel(original)


def _messages(records: list[logging.LogRecord]) -> list[str]:
    return [record.getMessage() for record in records]


def _request(query: str = "描述这张图片") -> AgentRequest:
    return AgentRequest(
        request_id="req-image-routing",
        channel_id="test-channel",
        session_id="session-image-routing",
        params={
            "query": query,
            "media_items": [
                {
                    "type": "image",
                    "path": "/attachments/diagram.png",
                    "filename": "diagram.png",
                    "mime_type": "image/png",
                }
            ],
        },
    )


def test_extraction_does_not_claim_context_window_injection():
    """The bug: extraction announced an injection it cannot know will happen."""
    request = _request()

    with _adapter_logs() as records:
        inputs = JiuWenSwarmDeepAdapter._prepare_multimodal_image_inputs(
            request, {"query": request.params["query"]}
        )

    assert inputs["_multimodal_image_files"]
    messages = _messages(records)
    assert messages, "extraction should still record that an attachment arrived"
    assert not any(
        "context" in m or "injection" in m for m in messages
    ), messages


def test_native_input_enabled_names_the_context_window_route():
    request = _request()
    inputs = JiuWenSwarmDeepAdapter._prepare_multimodal_image_inputs(
        request, {"query": request.params["query"]}
    )

    with _adapter_logs() as records:
        result = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
            request, inputs, enable_read_image_multimodal=True
        )

    # Native input keeps the files for the context-window rail.
    assert result["_multimodal_image_files"]
    messages = _messages(records)
    assert any(
        "Native image input enabled" in m and "context window" in m
        for m in messages
    ), messages


def test_native_input_disabled_names_the_tool_route():
    """The production case: the attachment reaches the model through the
    image-understanding tool only, and the log must say so."""
    request = _request()
    inputs = JiuWenSwarmDeepAdapter._prepare_multimodal_image_inputs(
        request, {"query": request.params["query"]}
    )

    with _adapter_logs() as records:
        result = JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
            request, inputs, enable_read_image_multimodal=False
        )

    # The fallback hands the files to the tool prompt and drops them from the
    # payload that feeds the context-window rail.
    assert "_multimodal_image_files" not in result
    assert "jiuwenswarm_image_tool_context" in result["query"]

    messages = _messages(records)
    assert any(
        "Native image input disabled" in m and "image-understanding tool" in m
        for m in messages
    ), messages
    assert not any("Native image input enabled" in m for m in messages), messages


def test_unbuildable_tool_prompt_warns_instead_of_claiming_a_route():
    """No text query to append to: neither route carries the attachment."""
    request = _request()
    inputs = JiuWenSwarmDeepAdapter._prepare_multimodal_image_inputs(
        request, {"query": None}
    )

    with _adapter_logs(logging.WARNING) as records:
        JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
            request, inputs, enable_read_image_multimodal=False
        )

    messages = _messages(records)
    assert any(
        "could not be built" in m and "not routed to the model" in m
        for m in messages
    ), messages


def test_request_without_attachments_stays_silent():
    request = AgentRequest(
        request_id="req-no-image",
        channel_id="test-channel",
        session_id="session-no-image",
        params={"query": "no attachment here"},
    )

    with _adapter_logs(logging.DEBUG) as records:
        inputs = JiuWenSwarmDeepAdapter._prepare_multimodal_image_inputs(
            request, {"query": request.params["query"]}
        )
        JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
            request, inputs, enable_read_image_multimodal=False
        )
        JiuWenSwarmDeepAdapter._prepare_react_image_tool_prompt(
            request, inputs, enable_read_image_multimodal=True
        )

    assert _messages(records) == []
