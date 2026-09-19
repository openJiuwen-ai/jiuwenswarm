"""Turn placement id from invocation; sticky xiaoyi_task_id stays in metadata."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.common.tools.send_html_card import SendHtmlCardToolkit
from jiuwenswarm.agents.harness.common.tools.turn_request_identity import (
    resolve_artifact_delivery,
    resolve_toolkit_delivery,
)
from jiuwenswarm.agents.harness.common.tools.xiaoyi_append_reference import (
    XiaoyiAppendReferenceToolkit,
)
from jiuwenswarm.common.invocation_context import (
    INVOCATION_CONTEXT_VERSION,
    InvocationContext,
    reset_current_invocation_context,
    set_current_invocation_context,
)

PC_UUID = "87ea6d78-1111-2222-3333-444444444444"
STICKY_TASK = "desktop_1a0b2adf1c0_a8aed9c02f67&12&7a8d&0"
DESKTOP_SESSION = "desktop_1a0b2adf1c0_a8aed9c02f67"
PHONE_SESSION = "sess-x-phone"
PHONE_TASK_HELMET = "e2808a23-8b38-4379-9171-ed2189838829&3&0834&0"
PHONE_TASK_BIKE = "e2808a23-8b38-4379-9171-ed2189838829&4&31ac&0"


@pytest.fixture(autouse=True)
def _reset_identity_state():
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()
    yield
    sfu._SENT_FILE_PATHS_BY_SESSION.clear()


def _invocation(*, request_id: str, session_id: str, channel_id: str = "xiaoyi") -> InvocationContext:
    return InvocationContext(
        version=INVOCATION_CONTEXT_VERSION,
        invocation_id="inv_test",
        request_id=request_id,
        session_id=session_id,
        channel_id=channel_id,
        chat_id=None,
    )


def test_resolve_uses_invocation_not_toolkit_fallback():
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION)
    )
    try:
        delivery = resolve_artifact_delivery(
            fallback_request_id=STICKY_TASK,
            fallback_session_id=DESKTOP_SESSION,
            fallback_channel_id="xiaoyi",
            metadata={"xiaoyi_task_id": STICKY_TASK},
        )
        assert delivery.turn_request_id == PC_UUID
        assert delivery.metadata["xiaoyi_task_id"] == STICKY_TASK
        assert delivery.channel_id == "xiaoyi"
    finally:
        reset_current_invocation_context(token)


def test_resolve_without_invocation_uses_fallback():
    delivery = resolve_artifact_delivery(
        fallback_request_id=PC_UUID,
        fallback_session_id=DESKTOP_SESSION,
        fallback_channel_id="desktop",
        metadata=None,
    )
    assert delivery.turn_request_id == PC_UUID
    assert delivery.channel_id == "desktop"
    assert "xiaoyi_task_id" not in delivery.metadata


def test_resolve_desktop_channel_with_sticky_task_forces_xiaoyi():
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION, channel_id="desktop")
    )
    try:
        delivery = resolve_artifact_delivery(
            fallback_request_id=PC_UUID,
            fallback_session_id=DESKTOP_SESSION,
            fallback_channel_id="desktop",
            metadata={"xiaoyi_task_id": STICKY_TASK},
        )
        assert delivery.turn_request_id == PC_UUID
        assert delivery.channel_id == "xiaoyi"
        assert delivery.metadata["xiaoyi_task_id"] == STICKY_TASK
    finally:
        reset_current_invocation_context(token)


def test_resolve_desktop_channel_with_xiaoyi_session_id_forces_xiaoyi():
    delivery = resolve_artifact_delivery(
        fallback_request_id=PC_UUID,
        fallback_session_id=DESKTOP_SESSION,
        fallback_channel_id="desktop",
        metadata={"xiaoyi_session_id": "conv_desktop_kanas"},
    )
    assert delivery.turn_request_id == PC_UUID
    assert delivery.channel_id == "xiaoyi"
    assert delivery.metadata["xiaoyi_session_id"] == "conv_desktop_kanas"


def test_overlay_live_desktop_sticky_forces_xiaoyi():
    from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
        _CRON_TOOL_BOUND,
        _CRON_TOOL_CHANNEL_ID,
        _CRON_TOOL_METADATA,
    )

    toolkit = sfu.SendFileToolkit(
        request_id=PC_UUID,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    tokens = (
        _CRON_TOOL_CHANNEL_ID.set("desktop"),
        _CRON_TOOL_METADATA.set(
            {
                "xiaoyi_task_id": STICKY_TASK,
                "xiaoyi_session_id": "conv_desktop_kanas",
            }
        ),
        _CRON_TOOL_BOUND.set(True),
    )
    inv = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION, channel_id="desktop")
    )
    try:
        delivery = resolve_toolkit_delivery(toolkit)
        assert delivery.turn_request_id == PC_UUID
        assert delivery.channel_id == "xiaoyi"
        assert delivery.metadata["xiaoyi_task_id"] == STICKY_TASK
    finally:
        reset_current_invocation_context(inv)
        _CRON_TOOL_BOUND.reset(tokens[2])
        _CRON_TOOL_METADATA.reset(tokens[1])
        _CRON_TOOL_CHANNEL_ID.reset(tokens[0])


def _run_send_file(toolkit, path: str):
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    with patch(
        "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
        return_value=mock_server,
    ), patch(
        "jiuwenswarm.server.runtime.session.session_history.append_history_record",
    ) as append:
        result = asyncio.run(toolkit.send_file(path))
    return result, mock_server, append


def test_send_file_desktop_sticky_task_uses_invocation_uuid(tmp_path):
    file_path = tmp_path / "kanas.png"
    file_path.write_text("img", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id=STICKY_TASK,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={
            "xiaoyi_task_id": STICKY_TASK,
            "xiaoyi_session_id": "conv_desktop_kanas",
        },
    )
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION)
    )
    try:
        result, mock_server, append = _run_send_file(toolkit, str(file_path))
    finally:
        reset_current_invocation_context(token)

    assert "Sent" in result
    assert append.call_args.kwargs["request_id"] == PC_UUID
    assert STICKY_TASK not in append.call_args.kwargs["request_id"]
    msg = mock_server.send_push.await_args.args[0]
    assert msg["request_id"] == PC_UUID
    assert msg["channel_id"] == "xiaoyi"
    assert msg["metadata"]["xiaoyi_task_id"] == STICKY_TASK
    assert mock_server.send_push.await_count == 1


def test_send_file_dirty_singleton_ignored_when_invocation_bound(tmp_path):
    file_path = tmp_path / "a.png"
    file_path.write_text("img", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id=PC_UUID,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    toolkit.request_id = STICKY_TASK
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION)
    )
    try:
        _, mock_server, append = _run_send_file(toolkit, str(file_path))
    finally:
        reset_current_invocation_context(token)
    assert append.call_args.kwargs["request_id"] == PC_UUID
    assert mock_server.send_push.await_args.args[0]["metadata"]["xiaoyi_task_id"] == STICKY_TASK


def test_send_file_phone_native_keeps_task_id(tmp_path):
    file_path = tmp_path / "a.png"
    file_path.write_text("img", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id=STICKY_TASK,
        session_id=PHONE_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    token = set_current_invocation_context(
        _invocation(request_id=STICKY_TASK, session_id=PHONE_SESSION)
    )
    try:
        _, mock_server, append = _run_send_file(toolkit, str(file_path))
    finally:
        reset_current_invocation_context(token)
    assert append.call_args.kwargs["request_id"] == STICKY_TASK
    assert mock_server.send_push.await_args.args[0]["request_id"] == STICKY_TASK


def test_send_file_pure_desktop_no_sticky(tmp_path):
    file_path = tmp_path / "a.png"
    file_path.write_text("img", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id=PC_UUID,
        session_id=DESKTOP_SESSION,
        channel_id="desktop",
    )
    _, mock_server, append = _run_send_file(toolkit, str(file_path))
    assert append.call_args.kwargs["request_id"] == PC_UUID
    msg = mock_server.send_push.await_args.args[0]
    assert msg["request_id"] == PC_UUID
    assert msg["channel_id"] == "desktop"
    assert "xiaoyi_task_id" not in (msg.get("metadata") or {})


def test_send_file_desktop_channel_sticky_forces_xiaoyi(tmp_path):
    file_path = tmp_path / "apple.png"
    file_path.write_text("img", encoding="utf-8")
    toolkit = sfu.SendFileToolkit(
        request_id=STICKY_TASK,
        session_id=DESKTOP_SESSION,
        channel_id="desktop",
        metadata={
            "xiaoyi_task_id": STICKY_TASK,
            "xiaoyi_session_id": "conv_desktop_kanas",
        },
    )
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION, channel_id="desktop")
    )
    try:
        result, mock_server, append = _run_send_file(toolkit, str(file_path))
    finally:
        reset_current_invocation_context(token)

    assert "Sent" in result
    assert append.call_args.kwargs["request_id"] == PC_UUID
    msg = mock_server.send_push.await_args.args[0]
    assert msg["request_id"] == PC_UUID
    assert msg["channel_id"] == "xiaoyi"
    assert msg["metadata"]["xiaoyi_task_id"] == STICKY_TASK


def test_update_runtime_context_caches_request_id_as_fallback():
    toolkit = sfu.SendFileToolkit(
        request_id=PC_UUID,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
    )
    toolkit.update_runtime_context(
        request_id=STICKY_TASK,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    assert toolkit.request_id == STICKY_TASK
    assert toolkit._request_metadata["xiaoyi_task_id"] == STICKY_TASK


def test_update_runtime_context_refreshes_metadata_for_delivery():
    toolkit = sfu.SendFileToolkit(
        request_id=PHONE_TASK_HELMET,
        session_id=PHONE_SESSION,
        channel_id="xiaoyi",
        metadata={
            "xiaoyi_task_id": PHONE_TASK_HELMET,
            "xiaoyi_session_id": PHONE_SESSION,
        },
    )
    toolkit.update_runtime_context(
        request_id="dirty-should-ignore",
        session_id=PHONE_SESSION,
        channel_id="xiaoyi",
        metadata={
            "xiaoyi_task_id": PHONE_TASK_BIKE,
            "xiaoyi_session_id": PHONE_SESSION,
        },
    )
    assert toolkit.request_id == "dirty-should-ignore"
    token = set_current_invocation_context(
        _invocation(request_id=PHONE_TASK_BIKE, session_id=PHONE_SESSION)
    )
    try:
        delivery = resolve_toolkit_delivery(toolkit)
    finally:
        reset_current_invocation_context(token)
    assert delivery.turn_request_id == PHONE_TASK_BIKE
    assert delivery.metadata["xiaoyi_task_id"] == PHONE_TASK_BIKE
    assert delivery.metadata["xiaoyi_session_id"] == PHONE_SESSION


def test_resolve_without_invocation_uses_refreshed_request_id():
    toolkit = sfu.SendFileToolkit(
        request_id=PC_UUID,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
    )
    toolkit.update_runtime_context(
        request_id=STICKY_TASK,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    delivery = resolve_toolkit_delivery(toolkit)
    assert delivery.turn_request_id == STICKY_TASK
    assert delivery.metadata["xiaoyi_task_id"] == STICKY_TASK


def test_html_card_uses_invocation_uuid():
    toolkit = SendHtmlCardToolkit(
        request_id=STICKY_TASK,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION)
    )
    try:
        with patch(
            "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
            return_value=mock_server,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ) as append:
            result = asyncio.run(
                toolkit.send_html_card(html_url="https://example.com/card.html")
            )
    finally:
        reset_current_invocation_context(token)
    assert "HTML card sent successfully" in result
    assert append.call_args.kwargs["request_id"] == PC_UUID
    assert mock_server.send_push.await_args.args[0]["metadata"]["xiaoyi_task_id"] == STICKY_TASK


def test_append_reference_uses_invocation_uuid():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id=STICKY_TASK,
        session_id=DESKTOP_SESSION,
        channel_id="xiaoyi",
        metadata={"xiaoyi_task_id": STICKY_TASK},
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    refs = [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]
    token = set_current_invocation_context(
        _invocation(request_id=PC_UUID, session_id=DESKTOP_SESSION)
    )
    try:
        with patch(
            "jiuwenswarm.server.agent_ws_server.AgentWebSocketServer.get_instance",
            return_value=mock_server,
        ), patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ) as append:
            result = asyncio.run(toolkit.append_reference(refs))
    finally:
        reset_current_invocation_context(token)
    assert "Sent 1 references" in result
    assert append.call_args.kwargs["request_id"] == PC_UUID
    assert mock_server.send_push.await_args.args[0]["metadata"]["xiaoyi_task_id"] == STICKY_TASK
