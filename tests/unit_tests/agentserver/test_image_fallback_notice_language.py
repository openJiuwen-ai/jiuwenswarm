# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The image-tool fallback notice must follow the configured language.

``_build_image_tool_fallback_notice`` writes text the user reads, so it belongs
in the language the rest of the reply is in. It was Chinese unconditionally.
Every other user-facing string the adapter produces goes through
``_resolve_runtime_language``, so an English deployment got one Chinese
paragraph in front of an English answer.

The notice also states why the attachment did not reach the model natively, and
``_native_image_input_status`` gives three different reasons for that. The
English copy has to keep those three apart, because they tell the reader
different things: configuration turned the capability off, a capability check
said no, or nothing is known yet.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)


def _request(request_id: str = "req-image-notice", session_id: str = "sess-image-notice") -> AgentRequest:
    return AgentRequest(
        request_id=request_id,
        channel_id="web",
        session_id=session_id,
        params={
            "query": "what does the picture say?",
            "media_items": [
                {
                    "type": "image",
                    "filename": "persisted.png",
                    "path": f"agent/sessions/{session_id}/uploads/persisted.png",
                    "mime_type": "image/png",
                }
            ],
        },
    )


def _model(model_name: str = "Fictional-1B") -> SimpleNamespace:
    return SimpleNamespace(model_config=SimpleNamespace(model_name=model_name))


@pytest.mark.parametrize(
    ("status", "expected"),
    [
        (
            "disabled",
            "Native image input is disabled in configuration for the current model "
            "(Fictional-1B).",
        ),
        (
            "unsupported",
            "The current model (Fictional-1B) interface did not accept native "
            "image input in the capability check.",
        ),
        (
            "unknown",
            "It is not confirmed whether the current model (Fictional-1B) "
            "interface accepts native image input.",
        ),
    ],
)
def test_english_notice_keeps_each_status_reason_apart(status, expected):
    """Each status the reader can see gives its own reason in English."""
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(  # pylint: disable=protected-access
        _request(),
        model=_model(),
        vision_tool_available=True,
        image_input_status=status,
        language="en",
    )

    assert notice is not None
    assert notice["content"] == expected + " An image understanding tool is used instead."
    assert notice["model_name"] == "Fictional-1B"


def test_chinese_notice_is_unchanged():
    """The Chinese copy, brackets included, stays what it was."""
    notice = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(  # pylint: disable=protected-access
        _request(),
        model=_model(),
        vision_tool_available=True,
        language="cn",
    )

    assert notice is not None
    assert notice["content"] == (
        "尚未确认当前模型（Fictional-1B）的接口是否支持原生图片输入。"
        "已切换为图片理解工具处理。"
    )


def test_an_unnamed_model_leaves_no_empty_label():
    """Chinese brackets and the English leading space are both label, not text."""
    english = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(  # pylint: disable=protected-access
        _request(),
        model=_model(""),
        vision_tool_available=True,
        language="en",
    )
    chinese = JiuWenSwarmDeepAdapter._build_image_tool_fallback_notice(  # pylint: disable=protected-access
        _request(),
        model=None,
        vision_tool_available=True,
        language="cn",
    )

    assert english is not None and chinese is not None
    assert english["content"].startswith("It is not confirmed whether the current model interface")
    assert chinese["content"].startswith("尚未确认当前模型的接口")
    assert "model_name" not in english
    assert "model_name" not in chinese


@pytest.mark.asyncio
async def test_the_streamed_notice_follows_the_configured_language(monkeypatch):
    """The ``chat.notice`` chunk carries the same language as the answer."""
    adapter = JiuWenSwarmDeepAdapter()
    adapter._instance = SimpleNamespace()  # pylint: disable=protected-access
    adapter._is_session_scoped_adapter = True  # pylint: disable=protected-access
    # A configured image-understanding tool, because that is the sentence
    # asserted below. Without one the notice says there is no image
    # understanding at all, which is a different sentence.
    adapter._vision_model_config = SimpleNamespace()  # pylint: disable=protected-access

    monkeypatch.setattr(adapter, "_has_valid_model_config", lambda _model_name="": True)
    monkeypatch.setattr(adapter, "_resolve_model_for_request", lambda _request: _model())
    monkeypatch.setattr(adapter, "_apply_model_to_react_agent", lambda _model, **_kwargs: None)
    monkeypatch.setattr(adapter, "_resolve_runtime_language", lambda: "en")
    monkeypatch.setattr(adapter, "_native_image_input_status", lambda *_args: "unknown")
    monkeypatch.setattr(adapter, "_write_runtime_state", lambda **_kwargs: None)

    async def _drain_team_inputs(_request, _inputs, _instance):
        if False:
            yield None

    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    monkeypatch.setattr(team_helpers, "process_team_message_stream", _drain_team_inputs)

    request = _request(
        request_id="req-team-image-notice", session_id="sess-team-image-notice"
    )
    request.params["mode"] = "team"
    request.is_stream = True

    notices = [
        chunk.payload
        async for chunk in adapter.process_message_stream_impl(
            request, {"query": "what does the picture say?"}
        )
        if isinstance(chunk.payload, dict)
        and chunk.payload.get("notice_type") == "image_tool_fallback"
    ]

    assert len(notices) == 1
    assert notices[0]["content"] == (
        "It is not confirmed whether the current model (Fictional-1B) "
        "interface accepts native image input. "
        "An image understanding tool is used instead."
    )
