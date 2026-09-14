# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the A2UI integration bridge.

The bridge keeps A2UI-specific request/config/fallback decisions out of the
core AgentServer and Gateway modules.
"""

from __future__ import annotations

import builtins

import pytest

from jiuwenswarm.server.runtime.a2ui import integration
from jiuwenswarm.server.runtime.a2ui.integration import (
    apply_non_web_text_fallback_to_payload,
    build_user_prompt_if_a2ui_event,
    get_a2ui_config_payload,
    is_a2ui_channel,
    validate_a2ui_config_update,
)


def test_a2ui_channel_policy_is_web_only():
    """A2UI should only be active for the controlled Web channel."""
    assert is_a2ui_channel("web") is True
    assert is_a2ui_channel("WEB") is True
    assert is_a2ui_channel("feishu") is False
    assert is_a2ui_channel("wechat") is False
    assert is_a2ui_channel(None) is False


def test_build_user_prompt_if_a2ui_event_disabled_returns_none(monkeypatch):
    """Disabled A2UI should leave client-event payloads to normal handling."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "false")
    event = {"type": "a2ui.client_event", "userAction": {"context": {"value": "ok"}}}

    assert build_user_prompt_if_a2ui_event(event, channel="web", language="zh") is None


def test_build_user_prompt_if_a2ui_event_enabled_mentions_context(monkeypatch):
    """Enabled A2UI should translate client events into model-readable prompts."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    monkeypatch.setattr(
        integration,
        "_build_a2ui_client_event_prompt",
        lambda content, channel, language: (
            f"{content['type']} on {channel}/{language}: context={content['userAction']['context']}"
        ),
    )
    event = {"type": "a2ui.client_event", "userAction": {"context": {"value": "ok"}}}

    prompt = build_user_prompt_if_a2ui_event(event, channel="web", language="zh")

    assert prompt is not None
    assert "a2ui.client_event" in prompt
    assert "context" in prompt


def test_build_user_prompt_if_a2ui_event_bypasses_non_web_channel(monkeypatch):
    """Non-Web channels should not perceive structured A2UI client events."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    event = {"type": "a2ui.client_event", "userAction": {"context": {"value": "ok"}}}

    assert build_user_prompt_if_a2ui_event(event, channel="feishu", language="zh") is None


def test_apply_non_web_text_fallback_skips_web_payload(monkeypatch):
    """Web messages must keep raw A2UI blocks for the frontend renderer."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    payload = {
        "event_type": "chat.final",
        "content": "hello <a2ui-json>[]</a2ui-json>",
    }

    assert apply_non_web_text_fallback_to_payload(payload, channel_id="web") is payload


def test_apply_non_web_text_fallback_bypasses_non_web_payload(monkeypatch):
    """Non-Web payloads should bypass A2UI fallback even when A2UI is enabled."""
    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")
    payload = {
        "event_type": "chat.final",
        "content": "hello <a2ui-json>[]</a2ui-json>",
    }

    assert apply_non_web_text_fallback_to_payload(payload, channel_id="telegram") is payload
    assert payload["content"] == "hello <a2ui-json>[]</a2ui-json>"


def test_message_handler_fallback_skips_a2ui_import_without_marker(monkeypatch):
    """Gateway hot path should not import A2UI when payload has no A2UI marker."""
    from jiuwenswarm.gateway.message_handler.message_handler import (
        apply_a2ui_text_fallback_to_gateway_payload,
    )

    real_import = builtins.__import__

    def guard_import(name, *args, **kwargs):
        if name == "jiuwenswarm.server.runtime.a2ui.integration":
            raise AssertionError("A2UI integration should not be imported")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guard_import)
    payload = {"event_type": "chat.final", "content": "plain text"}

    assert apply_a2ui_text_fallback_to_gateway_payload(payload, channel_id="telegram") is payload


def test_get_a2ui_config_payload_defaults():
    """Config payloads should expose only user-facing A2UI Web keys."""
    payload = get_a2ui_config_payload({"a2ui": {}})

    assert payload == {"a2ui_enabled": "false"}


def test_validate_a2ui_config_update_rejects_internal_keys():
    """Internal A2UI settings should not be mutable from the Web config page."""
    ok, update, error = validate_a2ui_config_update("a2ui_protocol_version", "0.9")

    assert ok is False
    assert update == {}
    assert "Unknown A2UI config key" in error


def test_validate_a2ui_config_update_maps_boolean_key():
    """Web config keys should map to the YAML keys owned by the A2UI config."""
    ok, update, error = validate_a2ui_config_update("a2ui_enabled", "false")

    assert ok is True
    assert update == {"enabled": False}
    assert error == ""


def test_normal_text_prompt_builder_keeps_string_flow(monkeypatch):
    """Non-A2UI string input should keep the normal prompt builder path."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    prompt = build_user_prompt("你好", files={}, channel="web", language="zh")

    assert '"content": "你好"' in prompt
    assert '"type": "user input"' in prompt


def test_a2ui_stream_probe_detects_split_protocol_marker():
    """Stream suppression should survive protocol markers split across chunks."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _extend_a2ui_stream_probe,
        _stream_probe_has_a2ui_marker,
    )

    probe = ""
    probe = _extend_a2ui_stream_probe(probe, "好的，开始整理。\n\nbeginRend")

    assert _stream_probe_has_a2ui_marker(probe) is True

    probe = _extend_a2ui_stream_probe(probe, "ering")

    assert _stream_probe_has_a2ui_marker(probe) is True


def test_a2ui_stream_probe_ignores_regular_begin_text():
    """Plain text containing begin-like words should keep normal streaming."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _extend_a2ui_stream_probe,
        _stream_probe_has_a2ui_marker,
    )

    probe = _extend_a2ui_stream_probe("", "I will begin by summarizing the mailbox.")

    assert _stream_probe_has_a2ui_marker(probe) is False


def test_a2ui_stream_probe_ignores_begin_sentence_at_line_start():
    """A normal sentence starting with begin should not look like A2UI."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _extend_a2ui_stream_probe,
        _stream_probe_has_a2ui_marker,
    )

    probe = _extend_a2ui_stream_probe("", "begin by summarizing the mailbox.")

    assert _stream_probe_has_a2ui_marker(probe) is False


def test_split_a2ui_stream_content_keeps_prefix_streamable():
    """Only A2UI protocol text should be suppressed from a mixed chunk."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _split_a2ui_stream_content

    split = _split_a2ui_stream_content(
        "",
        "浏览器代理已完成搜索和整理，现在为你展示结果。\n\nbeginRendering\n邮件整理完成",
    )

    assert split == (
        "浏览器代理已完成搜索和整理，现在为你展示结果。\n\n",
        "beginRendering\n邮件整理完成",
    )


def test_split_a2ui_stream_content_handles_partial_marker():
    """A partial marker long enough to be distinctive should suppress only the marker line."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _split_a2ui_stream_content

    split = _split_a2ui_stream_content("", "现在为你展示结果。\n\nbeginRend")

    assert split == ("现在为你展示结果。\n\n", "beginRend")


def test_split_a2ui_stream_content_ignores_short_tag_prefix():
    """A short '<a' fragment must not trigger A2UI suppression (false-positive guard)."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _split_a2ui_stream_content

    split = _split_a2ui_stream_content("", "现在为你展示结果。\n\n<a")

    assert split is None


def test_split_a2ui_stream_content_suppresses_long_tag_prefix():
    """A distinctive '<a2ui-js' prefix should trigger suppression."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _split_a2ui_stream_content

    split = _split_a2ui_stream_content("", "现在为你展示结果。\n\n<a2ui-js")

    assert split == ("现在为你展示结果。\n\n", "<a2ui-js")


def test_a2ui_pending_render_delta_stays_open():
    """The Web renderer shows its pending state only for an open A2UI block."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _A2UI_PENDING_RENDER_DELTA

    assert _A2UI_PENDING_RENDER_DELTA.startswith("<a2ui-json>")
    assert "</a2ui-json>" not in _A2UI_PENDING_RENDER_DELTA


def test_a2ui_repaired_final_chunk_keeps_session_binding():
    """The repaired final must replace the pending A2UI bubble in its session."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _make_a2ui_final_chunk

    chunk = _make_a2ui_final_chunk(
        request_id="req-a2ui",
        channel_id="web",
        session_id="sess-a2ui",
        content="<a2ui-json>[]</a2ui-json>",
    )

    assert chunk.payload == {
        "event_type": "chat.final",
        "session_id": "sess-a2ui",
        "content": "<a2ui-json>[]</a2ui-json>",
    }


def test_a2ui_processing_false_waits_for_repaired_final():
    """A2UI finalization must finish before the frontend closes its stream."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _should_defer_a2ui_processing_status,
    )

    payload = {
        "event_type": "chat.processing_status",
        "is_processing": False,
    }

    assert _should_defer_a2ui_processing_status(
        suppress_a2ui_stream=True,
        event_type="chat.processing_status",
        payload=payload,
    ) is True
    assert _should_defer_a2ui_processing_status(
        suppress_a2ui_stream=False,
        event_type="chat.processing_status",
        payload=payload,
    ) is False


def test_nested_stream_completion_waits_for_facade_post_processing():
    """The adapter terminal must not close the wire before A2UI finalization."""
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _normalize_nested_stream_chunk,
    )

    terminal = AgentResponseChunk(
        request_id="req-a2ui",
        channel_id="web",
        payload=None,
        is_complete=True,
    )

    assert _normalize_nested_stream_chunk(terminal) is None


def test_nested_terminal_event_remains_visible_without_closing_stream():
    """A meaningful terminal event is retained while the facade owns completion."""
    from jiuwenswarm.common.schema.agent import AgentResponseChunk
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _normalize_nested_stream_chunk,
    )

    terminal_error = AgentResponseChunk(
        request_id="req-error",
        channel_id="web",
        payload={"event_type": "chat.error", "error": "failed"},
        is_complete=True,
    )

    normalized = _normalize_nested_stream_chunk(terminal_error)

    assert normalized is not None
    assert normalized.payload == terminal_error.payload
    assert normalized.is_complete is False


def test_agent_prompt_builder_accepts_a2ui_client_event_dict(monkeypatch):
    """Structured Web A2UI events should bypass normal text prompt wrapping."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import build_user_prompt

    monkeypatch.setenv("JIUWENSWARM_A2UI_ENABLED", "true")

    prompt = build_user_prompt(
        {
            "type": "a2ui.client_event",
            "protocolVersion": "0.8",
            "event": {
                "userAction": {
                    "name": "submit_form",
                    "surfaceId": "surface-1",
                    "sourceComponentId": "submit",
                    "context": {"name": "张三"},
                },
            },
        },
        files={},
        channel="web",
        language="zh",
    )

    assert "你收到了一次 A2UI 组件交互" in prompt
    assert "submit_form" in prompt
    assert "张三" in prompt


@pytest.mark.asyncio
async def test_finalize_assistant_response_if_a2ui_noops_when_config_lookup_fails(monkeypatch):
    """A2UI finalization must not break the core agent response path."""
    def fail_config_lookup():
        raise RuntimeError("config unavailable")

    async def repair_call(prompt: str):
        raise AssertionError("repair should not run when A2UI config is unavailable")

    monkeypatch.setattr(integration, "_get_runtime_a2ui_config", fail_config_lookup)

    content = "<a2ui-json>[]</a2ui-json>"
    result = await integration.finalize_assistant_response_if_a2ui(
        content,
        user_query="generate a form",
        request_id="req-config-error",
        repair_call=repair_call,
    )

    assert result == content


@pytest.mark.asyncio
async def test_finalize_assistant_response_if_a2ui_bypasses_non_web_channel(monkeypatch):
    """Non-Web responses should not run A2UI config lookup, validation, or repair."""
    def fail_config_lookup():
        raise AssertionError("non-Web channel should bypass A2UI config lookup")

    async def repair_call(prompt: str):
        raise AssertionError("repair should not run for non-Web channels")

    monkeypatch.setattr(integration, "_get_runtime_a2ui_config", fail_config_lookup)

    content = "<a2ui-json>[]</a2ui-json>"
    result = await integration.finalize_assistant_response_if_a2ui(
        content,
        channel="feishu",
        user_query="generate a form",
        request_id="req-non-web",
        repair_call=repair_call,
    )

    assert result == content


def test_partial_marker_ignores_short_data_token():
    """'data' alone must not trigger A2UI suppression (ECharts false-positive)."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("some content\ndata") is False


def test_partial_marker_ignores_data_with_array():
    """'data: [1,2,3]' must not trigger A2UI suppression."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("chart.setOption({\ndata: [1, 2, 3]\n})") is False


def test_partial_marker_ignores_aside_tag():
    """'<aside>' must not trigger A2UI suppression (HTML false-positive)."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("content\n<aside>") is False


def test_partial_marker_ignores_beginning_word():
    """'beginning' must not trigger A2UI suppression."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("in the beginning") is False


def test_partial_marker_ignores_delete_token():
    """'delete' alone must not trigger A2UI suppression."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("code\n\ndelete") is False


def test_partial_marker_ignores_surface_token():
    """'surface' alone must not trigger A2UI suppression."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("render\nsurface") is False


def test_partial_marker_triggers_on_long_dataModel_prefix():
    """'dataModel' is long enough to be distinctive and should trigger."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _looks_like_partial_a2ui_marker,
    )

    assert _looks_like_partial_a2ui_marker("content\ndataModel") is True
