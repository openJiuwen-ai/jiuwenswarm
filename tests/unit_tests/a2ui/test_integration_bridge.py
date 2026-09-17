# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the A2UI integration bridge.

The bridge keeps A2UI-specific request/config/fallback decisions out of the
core AgentServer and Gateway modules.
"""

from __future__ import annotations

import asyncio
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
    probe = _extend_a2ui_stream_probe(probe, "好的，开始整理。\n\nbeg")

    assert _stream_probe_has_a2ui_marker(probe) is True

    probe = _extend_a2ui_stream_probe(probe, "inRend")

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


def test_persistent_team_stream_does_not_buffer_member_a2ui():
    """Persistent Team streams must not use request-wide A2UI buffering."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import (
        _should_probe_a2ui_stream,
    )

    assert _should_probe_a2ui_stream(is_team_mode=True) is False
    assert _should_probe_a2ui_stream(is_team_mode=False) is True


def test_team_a2ui_block_closes_across_chunks_without_blocking_other_member():
    """Only the emitting member is buffered until its closing tag arrives."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    analyst = {"rid": 7, "role": "teammate", "member_name": "analyst"}
    reviewer = {"rid": 7, "role": "teammate", "member_name": "reviewer"}

    first = blocks.consume(analyst, "chat.delta", "结果如下：\n<a2")
    other = blocks.consume(reviewer, "chat.delta", "审查任务已经完成。")
    middle = blocks.consume(analyst, "chat.delta", "ui-json>[{")
    closed = blocks.consume(analyst, "chat.delta", "}]</a2ui-json>")

    assert first is not None
    assert first.passthrough == "结果如下：\n"
    assert first.suppress is True
    assert other is None
    assert middle is not None
    assert middle.suppress is True
    assert closed is not None
    assert closed.raw_block == "<a2ui-json>[{}]</a2ui-json>"


def test_team_a2ui_closed_block_preserves_prefix_and_trailing_text():
    """Text surrounding a complete local block remains streamable."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    payload = {"rid": 8, "role": "leader"}

    decision = blocks.consume(
        payload,
        "chat.delta",
        "开始展示。<a2ui-json>[]</a2ui-json>展示完成。",
    )

    assert decision is not None
    assert decision.passthrough == "开始展示。"
    assert decision.raw_block == "<a2ui-json>[]</a2ui-json>"
    assert decision.trailing == "展示完成。"


def test_team_a2ui_final_reuses_repaired_closed_block():
    """The later answer frame reuses repair output instead of repairing twice."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    payload = {"rid": 9, "role": "teammate", "member_name": "writer"}
    raw_block = "<a2ui-json>[invalid]</a2ui-json>"
    repaired_block = "<a2ui-json>[]</a2ui-json>"
    closed = blocks.consume(payload, "chat.delta", raw_block)
    assert closed is not None
    blocks.remember_finalized(closed.key, raw_block, repaired_block)

    final = blocks.consume(payload, "chat.final", f"说明。{raw_block}")

    assert final is not None
    assert final.replacement == f"说明。{repaired_block}"
    assert final.finalize_whole_event is False


def test_team_a2ui_final_detects_block_not_seen_in_deltas():
    """A new block in chat.final is finalized after known blocks are replaced."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    payload = {"rid": 9, "role": "teammate", "member_name": "writer"}
    raw_block = "<a2ui-json>[invalid-a]</a2ui-json>"
    repaired_block = "<a2ui-json>[]</a2ui-json>"
    new_block = "<a2ui-json>[invalid-b]</a2ui-json>"
    closed = blocks.consume(payload, "chat.delta", raw_block)
    assert closed is not None
    blocks.remember_finalized(closed.key, raw_block, repaired_block)

    final = blocks.consume(
        payload,
        "chat.final",
        f"说明。{raw_block}{new_block}",
    )

    assert final is not None
    assert final.raw_block == f"说明。{repaired_block}{new_block}"
    assert final.finalize_whole_event is True


def test_team_a2ui_final_recognizes_already_repaired_block():
    """A known finalized block must not be mistaken for a new block."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    payload = {"rid": 9, "role": "teammate", "member_name": "writer"}
    raw_block = "<a2ui-json>[invalid]</a2ui-json>"
    repaired_block = "<a2ui-json>[]</a2ui-json>"
    closed = blocks.consume(payload, "chat.delta", raw_block)
    assert closed is not None
    blocks.remember_finalized(closed.key, raw_block, repaired_block)

    final = blocks.consume(payload, "chat.final", repaired_block)

    assert final is None


def test_team_a2ui_member_final_is_fallback_for_missing_close_tag():
    """An unclosed block is finalized at the member boundary, not Team end."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    payload = {"rid": 10, "role": "teammate", "member_name": "writer"}
    held = blocks.consume(payload, "chat.delta", "<a2ui-json>[{}]")

    final = blocks.consume(payload, "chat.final", "说明。<a2ui-json>[{}]")

    assert held is not None
    assert held.suppress is True
    assert final is not None
    assert final.raw_block == "说明。<a2ui-json>[{}]"
    assert final.finalize_whole_event is True


def test_team_a2ui_partial_tag_false_alarm_is_released_locally():
    """A split prefix that is not A2UI must be returned without data loss."""
    from jiuwenswarm.server.runtime.a2ui.runtime.team_stream import TeamA2UIBlockBuffer

    blocks = TeamA2UIBlockBuffer()
    payload = {"rid": 11, "role": "leader"}
    held = blocks.consume(payload, "chat.delta", "链接：<a")
    released = blocks.consume(payload, "chat.delta", " href='https://example.com'>")

    assert held is not None
    assert held.passthrough == "链接："
    assert released is not None
    assert released.passthrough == "<a href='https://example.com'>"


@pytest.mark.asyncio
async def test_team_a2ui_repair_does_not_block_other_member(monkeypatch):
    """A slow local repair must not delay an unrelated teammate event."""
    from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
    from jiuwenswarm.server.runtime.agent_adapter import team_helpers

    raw_block = "<a2ui-json>[invalid]</a2ui-json>"
    repaired_block = "<a2ui-json>[]</a2ui-json>"
    new_block = "<a2ui-json>[new-invalid]</a2ui-json>"
    final_repaired = f"{repaired_block}<a2ui-json>[new-valid]</a2ui-json>"

    class FakeSessionManager:
        @staticmethod
        def get_session_id(session_id=None):
            return session_id or "default"

    class FakeAdapter:
        _instance = None

        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            yield AgentResponseChunk(
                request_id="req-team-a2ui",
                channel_id="web",
                payload={
                    "event_type": "chat.delta",
                    "content": raw_block,
                    "rid": 12,
                    "role": "teammate",
                    "member_name": "writer",
                },
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-team-a2ui",
                channel_id="web",
                payload={
                    "event_type": "chat.delta",
                    "content": "reviewer finished",
                    "rid": 12,
                    "role": "teammate",
                    "member_name": "reviewer",
                },
                is_complete=False,
            )
            yield AgentResponseChunk(
                request_id="req-team-a2ui",
                channel_id="web",
                payload={
                    "event_type": "chat.final",
                    "content": f"{raw_block}{new_block}",
                    "rid": 12,
                    "role": "teammate",
                    "member_name": "writer",
                },
                is_complete=False,
            )

    async def fake_finalize(content, **_kwargs):
        if content == raw_block:
            await asyncio.sleep(0.02)
            return repaired_block
        if content == f"{repaired_block}{new_block}":
            return final_repaired
        return content

    async def has_team_runtime(*_args, **_kwargs):
        return True

    monkeypatch.setattr(interface_module, "SessionManager", FakeSessionManager)
    monkeypatch.setattr(interface_module, "get_config", lambda: {})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    monkeypatch.setattr(
        interface_module,
        "_schedule_symphony_session_feedback",
        lambda *_args, terminal_status="success": None,
    )
    monkeypatch.setattr(interface_module, "finalize_assistant_response_if_a2ui", fake_finalize)
    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda self, mode="agent": FakeAdapter(),
    )
    monkeypatch.setattr(team_helpers, "_team_session_has_runtime", has_team_runtime)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.team.get_team_manager",
        lambda _channel_id: object(),
    )

    request = AgentRequest(
        request_id="req-team-a2ui",
        channel_id="web",
        session_id="sess-team-a2ui",
        params={"query": "render", "mode": "team"},
        is_stream=True,
    )
    chunks = [
        chunk
        async for chunk in interface_module.JiuWenSwarm().process_message_stream(request)
    ]
    contents = [
        str(chunk.payload.get("content"))
        for chunk in chunks
        if isinstance(chunk.payload, dict) and chunk.payload.get("content")
    ]

    assert contents.index("reviewer finished") < contents.index(final_repaired)
    assert raw_block not in contents
    assert new_block not in contents


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
    """A partial marker should suppress only the marker line."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _split_a2ui_stream_content

    split = _split_a2ui_stream_content("", "现在为你展示结果。\n\nbeg")

    assert split == ("现在为你展示结果。\n\n", "beg")


def test_split_a2ui_stream_content_suppresses_two_character_tag_prefix():
    """A tokenizer chunk containing only '<a' must not leak into visible text."""
    from jiuwenswarm.server.runtime.agent_adapter.interface import _split_a2ui_stream_content

    split = _split_a2ui_stream_content("", "现在为你展示结果。\n\n<a")

    assert split == ("现在为你展示结果。\n\n", "<a")


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
