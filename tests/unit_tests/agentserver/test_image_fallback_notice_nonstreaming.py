# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The image-tool fallback notice must reach non-streaming callers too.

``_build_image_tool_fallback_notice`` tells the user that the answering model
cannot read the attachment and that an image-understanding tool was used
instead. Only the streaming paths yielded it, so a non-streaming request --
what chat connectors issue -- got the degraded answer with no explanation.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)

ANSWER = "这是一张流程图。"


class _Stream:
    """Minimal stand-in for the runtime's interaction stream."""

    def __init__(self, chunks):
        self._chunks = list(chunks)
        self.closed = False

    def __aiter__(self):
        return self

    async def __anext__(self):
        if not self._chunks:
            raise StopAsyncIteration
        return self._chunks.pop(0)

    async def close(self, abort_active_round: bool = True):
        self.closed = True


def _request(with_image: bool = True) -> AgentRequest:
    params = {"query": "这是什么？", "mode": "agent"}
    if with_image:
        params["media_items"] = [
            {
                "type": "image",
                "path": "/attachments/flow.png",
                "filename": "flow.png",
                "mime_type": "image/png",
            }
        ]
    return AgentRequest(
        request_id="req-notice",
        channel_id="discord",
        session_id="session-notice",
        params=params,
    )


def _adapter(model_name: str, *, native_image_input: bool) -> JiuWenSwarmDeepAdapter:
    adapter = object.__new__(JiuWenSwarmDeepAdapter)
    adapter._is_session_scoped_adapter = True
    adapter._parent_session_id = None
    adapter._config_cache = {"enable_read_image_multimodal": native_image_input}
    adapter._vision_model_config = None
    adapter._stream_event_rail = None
    adapter._instance = MagicMock()
    adapter._instance.attach_output = AsyncMock(
        return_value=_Stream(
            [SimpleNamespace(type="answer", payload={"content": ANSWER})]
        )
    )
    adapter._instance.send_input = AsyncMock()

    adapter._has_valid_model_config = MagicMock(return_value=True)
    adapter._handle_slash_command = AsyncMock(return_value=None)
    adapter._bind_runtime_cron_context = MagicMock(return_value={})
    adapter._runtime_cron_tool_context = MagicMock()
    adapter._reset_runtime_cron_context = MagicMock()
    adapter._resolve_model_for_request = MagicMock(
        return_value=SimpleNamespace(
            model_config=SimpleNamespace(model_name=model_name)
        )
    )
    adapter._apply_model_to_react_agent = MagicMock()
    adapter._mark_session_active = MagicMock()
    adapter._unmark_session_active = MagicMock()
    adapter._register_session_agent_task = MagicMock()
    adapter._unregister_session_agent_task = MagicMock()
    adapter._update_runtime_config = AsyncMock()
    adapter._wants_attach_goal = MagicMock(return_value=False)
    adapter._should_inject_into_existing_interaction = MagicMock(return_value=False)
    adapter._resolve_input_dispatch_mode = MagicMock(return_value="chat")
    adapter._loaded_agent_template = None
    adapter._loaded_plugins = {}
    return adapter


@pytest.fixture
def quiet_runtime(monkeypatch):
    """Stub the process-wide hooks the adapter reaches for around a run."""
    module = "jiuwenswarm.server.runtime.agent_adapter.interface_deep"
    monkeypatch.setattr(f"{module}.setup_permission_context", lambda request: None)
    monkeypatch.setattr(f"{module}.cleanup_permission_context", lambda token: None)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.agent_observability.sync_agent_observability",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.agent_observability.mark_single_agent_team",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.agent_observability.open_agent_run_span",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.agent_observability.close_agent_run_span",
        lambda *args, **kwargs: None,
    )


@pytest.mark.asyncio
async def test_notice_reaches_a_non_streaming_caller(quiet_runtime):
    """The bug: a model without native image input answered from a tool
    summary and the payload said nothing about it."""
    adapter = _adapter("glm-4", native_image_input=False)
    request = _request()

    response = await adapter.process_message_impl(request, dict(request.params))

    assert response.ok
    payload = response.payload
    assert "不支持原生图片理解" in payload["content"]
    assert payload["content"].endswith(ANSWER)
    # The answering model is named in the notice, so the reader can tell which
    # model could not read the attachment.
    assert "glm-4" in payload["content"]

    # The notice travels in ``content`` and nowhere else. No key is added to the
    # payload: every non-streaming consumer renders ``content`` only, so a
    # second key would be surface with no reader.
    assert set(payload) == {"content"}


@pytest.mark.asyncio
async def test_no_notice_when_the_model_reads_the_image_itself(quiet_runtime):
    adapter = _adapter("qwen-vl-max", native_image_input=True)
    request = _request()

    response = await adapter.process_message_impl(request, dict(request.params))

    assert response.payload == {"content": ANSWER}


@pytest.mark.asyncio
async def test_no_notice_without_an_attachment(quiet_runtime):
    """Nothing to fall back from: a text-only turn stays untouched."""
    adapter = _adapter("glm-4", native_image_input=False)
    request = _request(with_image=False)

    response = await adapter.process_message_impl(request, dict(request.params))

    assert response.payload == {"content": ANSWER}


@pytest.mark.asyncio
async def test_notice_stands_alone_when_the_run_produces_no_text(quiet_runtime):
    adapter = _adapter("glm-4", native_image_input=False)
    adapter._instance.attach_output = AsyncMock(return_value=_Stream([]))
    request = _request()

    response = await adapter.process_message_impl(request, dict(request.params))

    payload = response.payload
    assert payload["content"].strip().endswith("已切换为图片理解工具处理。")
    assert set(payload) == {"content"}


ERROR_TEXT = "模型调用失败"


def _error_chunk():
    """An ``answer`` chunk carrying a terminal failure rather than a reply."""
    return SimpleNamespace(
        type="answer", payload={"result_type": "error", "output": ERROR_TEXT}
    )


@pytest.mark.asyncio
async def test_notice_reaches_the_error_reply_that_still_carries_an_answer(
    quiet_runtime,
):
    """The second exit renders user-facing text too.

    A run that emits text and then fails returns ``ok=False`` with both the
    error and the partial answer. That answer came from the image tool just
    like any other, so it needs the same notice; the success return is not the
    only place a reply carries text.
    """
    adapter = _adapter("glm-4", native_image_input=False)
    adapter._instance.attach_output = AsyncMock(
        return_value=_Stream(
            [
                SimpleNamespace(type="answer", payload={"content": ANSWER}),
                _error_chunk(),
            ]
        )
    )
    request = _request()

    response = await adapter.process_message_impl(request, dict(request.params))

    assert response.ok is False
    payload = response.payload
    assert payload["error"] == ERROR_TEXT
    assert "不支持原生图片理解" in payload["content"]
    assert payload["content"].endswith(ANSWER)
    # The error reply keeps exactly the keys it had; the notice rides in the
    # text that was already there.
    assert set(payload) == {"error", "content"}


@pytest.mark.asyncio
async def test_no_notice_on_an_error_reply_with_no_answer(quiet_runtime):
    """Nothing to explain: a failure that produced no text stays a bare error.

    The notice describes where an answer came from, so a reply with no answer
    must not gain a ``content`` key just to carry it -- that would put the
    notice where a consumer expects the model's output.
    """
    adapter = _adapter("glm-4", native_image_input=False)
    adapter._instance.attach_output = AsyncMock(
        return_value=_Stream([_error_chunk()])
    )
    request = _request()

    response = await adapter.process_message_impl(request, dict(request.params))

    assert response.ok is False
    assert response.payload == {"error": ERROR_TEXT}
