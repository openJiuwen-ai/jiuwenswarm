import asyncio
import json
import sys
from contextlib import contextmanager
from types import ModuleType
from unittest.mock import AsyncMock, MagicMock, patch

from jiuwenswarm.agents.harness.common.tools.xiaoyi_append_reference import (
    XiaoyiAppendReferenceToolkit,
)
from jiuwenswarm.common.xiaoyi_reference import (
    build_a2a_reference_items,
    coerce_references,
    resolve_append_reference_enabled,
)
from jiuwenswarm.common.config import get_progressive_tool_always_visible_tools
from jiuwenswarm.server.gateway_push.wire import build_server_push_wire
from jiuwenswarm.server.runtime.session import session_history
from jiuwenswarm.server.wire_truncate import (
    _HISTORY_COLLAPSE_KEEP_KEYS,
    _HISTORY_WIRE_RECORD_MAX_BYTES,
    _sanitize_history_record_for_wire,
    _select_history_record_page,
)


@contextmanager
def _patch_agent_server(mock_server):
    module_name = "jiuwenswarm.server.agent_ws_server"
    previous = sys.modules.get(module_name)
    had_previous = module_name in sys.modules
    module = ModuleType(module_name)
    module.AgentWebSocketServer = MagicMock()
    module.AgentWebSocketServer.get_instance.return_value = mock_server
    sys.modules[module_name] = module
    try:
        yield mock_server
    finally:
        if had_previous:
            sys.modules[module_name] = previous
        else:
            sys.modules.pop(module_name, None)


def test_patch_agent_server_does_not_evict_new_modules():
    module_name = "jiuwenswarm._test_patch_agent_server_keepalive"
    sentinel = ModuleType(module_name)
    try:
        with _patch_agent_server(MagicMock()):
            sys.modules[module_name] = sentinel
        assert sys.modules[module_name] is sentinel
    finally:
        sys.modules.pop(module_name, None)


def test_coerce_references_accepts_list_and_json_string():
    row = {
        "title": "标题",
        "url": "https://example.com/a",
        "source": "web_search",
        "name": "示例站",
    }
    assert coerce_references([row])[0]["url"] == "https://example.com/a"
    parsed = coerce_references(
        '[{"title":"标题","url":"https://example.com/a","source":"web_search","name":"示例站"}]'
    )
    assert len(parsed) == 1
    assert coerce_references([]) == []
    assert coerce_references([{"title": "缺字段"}]) == []


def test_coerce_references_accepts_wrapper_and_reference_data_object():
    protocol_item = {
        "params": {"name": "百度百科", "source": "web_search"},
        "card": {
            "type": "leftPictureRightText",
            "params": {
                "title": "标题",
                "subTitle": "百度百科",
                "link": {
                    "webLink": {
                        "startMode": 0,
                        "url": "https://example.com/a",
                    }
                },
                "imageInfo": {"small": {"url": "https://example.com/logo.png"}},
            },
        },
    }
    refs = coerce_references({"references": [protocol_item]})
    assert refs == [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "百度百科",
            "imageUrl": "https://example.com/logo.png",
        }
    ]


def test_coerce_references_rejects_protocol_items_with_missing_url():
    protocol_item = {
        "params": {"name": "站点", "source": "web_search"},
        "card": {
            "type": "leftPictureRightText",
            "params": {
                "title": "标题",
                "link": {"webLink": {"startMode": 0}},
                "imageInfo": {"small": {}},
            },
        },
    }
    assert coerce_references([protocol_item]) == []


def test_coerce_references_falls_back_to_flat_fields_for_incomplete_protocol_item():
    item = {
        "title": "扁平标题",
        "url": "https://example.com/flat",
        "source": "web_search",
        "name": "扁平站点",
        "params": {"name": "协议站点", "source": "web_search"},
        "card": {"params": {"title": "协议标题"}},
    }
    assert coerce_references([item]) == [
        {
            "title": "扁平标题",
            "url": "https://example.com/flat",
            "source": "web_search",
            "name": "扁平站点",
        }
    ]


def test_coerce_references_drops_empty_protocol_image_url():
    item = {
        "params": {"name": "站点", "source": "web_search"},
        "card": {
            "params": {
                "title": "标题",
                "link": {
                    "webLink": {
                        "startMode": 0,
                        "url": "https://example.com/a",
                    }
                },
                "imageInfo": {"small": {}},
            }
        },
    }
    refs = coerce_references([item])
    assert refs[0]["url"] == "https://example.com/a"
    assert "imageUrl" not in refs[0]


def test_coerce_references_rejects_non_http_url_schemes():
    base = {
        "title": "标题",
        "source": "web_search",
        "name": "示例站",
    }
    parsed = coerce_references(
        [
            {**base, "url": "javascript:alert(1)"},
            {**base, "url": "data:text/html,<script>alert(1)</script>"},
            {**base, "url": "file:///etc/passwd"},
            {
                **base,
                "url": "https://example.com/a",
                "imageUrl": "data:image/svg+xml,<svg/>",
            },
        ]
    )

    assert parsed == [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]


def test_coerce_references_normalizes_missing_url_scheme():
    base = {
        "title": "标题",
        "source": "web_search",
        "name": "示例站",
    }
    parsed = coerce_references(
        [
            {**base, "url": "www.example.com/a"},
            {**base, "url": "//example.com/b"},
        ]
    )

    assert [item["url"] for item in parsed] == [
        "https://www.example.com/a",
        "https://example.com/b",
    ]


def test_append_reference_defaults_and_overrides_by_channel():
    assert resolve_append_reference_enabled(None, "desktop")
    assert resolve_append_reference_enabled(None, "xiaoyi")
    assert not resolve_append_reference_enabled(None, "feishu")
    config = {
        "channels": {
            "desktop": {"append_reference_allowed": False},
            "feishu": {"append_reference_allowed": True},
        }
    }
    assert not resolve_append_reference_enabled(config, "desktop")
    assert resolve_append_reference_enabled(config, "feishu")
    assert not resolve_append_reference_enabled(
        {"channels": {"desktop": {"append_reference_allowed": "false"}}},
        "desktop",
    )
    assert resolve_append_reference_enabled(
        {"channels": {"feishu": {"append_reference_allowed": "on"}}},
        "feishu",
    )


def test_reference_tool_is_visible_for_old_progressive_config():
    config = {
        "progressive_tool_always_visible_tools": [
            "todo_create",
            "send_file_to_user",
        ]
    }
    visible = get_progressive_tool_always_visible_tools(config)
    assert visible[0] == "xiaoyi_append_reference"
    assert visible.count("xiaoyi_append_reference") == 1


def test_reference_payload_survives_history_collapse():
    assert "references" in _HISTORY_COLLAPSE_KEEP_KEYS
    references = [
        {
            "title": "标题" * 20_000,
            "url": "https://example.com/" + "a" * 20_000,
            "source": "web_search",
            "name": "站点",
            "imageUrl": "https://example.com/" + "b" * 20_000,
        }
        for _ in range(100)
    ]
    sanitized = _sanitize_history_record_for_wire(
        {
            "id": "reference-large",
            "role": "assistant",
            "event_type": "chat.reference",
            "content": "",
            "references": references,
        }
    )
    assert len(json.dumps(sanitized, ensure_ascii=False).encode("utf-8")) <= (
        _HISTORY_WIRE_RECORD_MAX_BYTES
    )
    assert sanitized["references"]
    assert sanitized["references_truncated"] is True

    string_sanitized = _sanitize_history_record_for_wire(
        {
            "id": "reference-string",
            "role": "assistant",
            "event_type": "chat.reference",
            "references": "x" * 100_000,
        }
    )
    assert (
        len(json.dumps(string_sanitized, ensure_ascii=False).encode("utf-8"))
        <= _HISTORY_WIRE_RECORD_MAX_BYTES
    )


def test_reference_payload_keeps_array_on_tiny_history_budget():
    references = [
        {
            "title": "标题" * 2_000,
            "url": "https://example.com/" + "a" * 2_000,
            "source": "web_search",
        }
        for _ in range(20)
    ]
    records = [
        {
            "id": "reference-tiny-budget",
            "role": "assistant",
            "event_type": "chat.reference",
            "content": "x" * 100_000,
            "references": references,
        }
    ]

    page, next_cursor = _select_history_record_page(
        records,
        cursor=0,
        limit=1,
        max_bytes=2_048,
        session_id="reference-session",
    )

    assert next_cursor == 1
    assert isinstance(page[0]["references"], list)
    assert page[0]["references"] == []
    assert page[0]["references_truncated"] is True
    assert len(json.dumps(page[0], ensure_ascii=False).encode("utf-8")) <= 2_048


def test_chat_reference_with_items_is_persistable():
    assert session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.reference",
        extra={
            "references": [
                {
                    "title": "标题",
                    "url": "https://example.com/a",
                    "source": "web_search",
                    "name": "示例站",
                }
            ]
        },
    )
    assert not session_history._has_persistable_assistant_payload(
        content_text="",
        event_type="chat.reference",
        extra={"references": []},
    )


def test_append_history_record_reports_skipped_sessions():
    kwargs = {
        "request_id": "r1",
        "channel_id": "desktop",
        "role": "assistant",
        "event_type": "chat.reference",
        "content": "",
        "timestamp": 1.0,
        "extra": {
            "references": [
                {
                    "title": "标题",
                    "url": "https://example.com/a",
                    "source": "web_search",
                    "name": "示例站",
                }
            ]
        },
    }
    assert session_history.append_history_record(session_id="default", **kwargs) is False
    assert session_history.append_history_record(
        session_id="heartbeat-test", **kwargs
    ) is False


def test_append_history_record_wait_persists_before_returning(tmp_path, monkeypatch):
    sessions_dir = tmp_path / "agent" / "sessions"
    monkeypatch.setattr(
        session_history,
        "get_agent_sessions_dir",
        lambda: sessions_dir,
    )

    persisted = session_history.append_history_record(
        session_id="sync-reference",
        request_id="r1",
        channel_id="desktop",
        role="assistant",
        event_type="chat.reference",
        content="",
        timestamp=1.0,
        extra={
            "references": [
                {
                    "title": "标题",
                    "url": "https://example.com/a",
                    "source": "web_search",
                    "name": "示例站",
                }
            ]
        },
        wait=True,
    )

    assert persisted is True
    history_path = sessions_dir / "sync-reference" / "history.jsonl"
    assert history_path.exists()
    assert json.loads(history_path.read_text(encoding="utf-8"))["references"]


def test_build_a2a_reference_items_matches_phone_card():
    items = build_a2a_reference_items(
        [
            {
                "title": "标题",
                "url": "https://example.com/a",
                "source": "web_search",
                "name": "百度百科",
                "imageUrl": "https://example.com/logo.png",
            }
        ]
    )
    assert items[0]["params"] == {"name": "百度百科", "source": "web_search"}
    params = items[0]["card"]["params"]
    assert items[0]["card"]["type"] == "leftPictureRightText"
    assert params["title"] == "标题"
    assert params["subTitle"] == "百度百科"
    assert params["link"]["webLink"] == {"startMode": 0, "url": "https://example.com/a"}
    assert params["imageInfo"]["small"]["url"] == "https://example.com/logo.png"


def test_append_reference_send_push(tmp_path):
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="xiaoyi",
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

    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ),
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    assert "Sent 1 references" in result
    assert mock_server.send_push.await_count == 1
    payload = mock_server.send_push.await_args.args[0]["payload"]
    assert payload["event_type"] == "chat.reference"
    assert payload["references"][0]["url"] == "https://example.com/a"
    wire = build_server_push_wire(mock_server.send_push.await_args.args[0])
    assert wire["body"]["event_type"] == "chat.reference"
    assert wire["body"]["delta_kind"] == "custom"
    assert wire["body"]["delta"]["references"] == refs
    assert wire["channel"] == "xiaoyi"
    assert wire["metadata"]["_jiuwenswarm_server_push"] is True
    assert wire["is_final"] is False
    result_payload = json.loads(result)
    assert result_payload["success"] is True
    assert result_payload["reference_event_sent"] is True
    assert result_payload["references"] == refs


def test_append_reference_desktop_result_supports_legacy_frontend():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-desktop",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    refs = [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
            "imageUrl": "https://example.com/logo.png",
        }
    ]

    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ),
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    sent = mock_server.send_push.await_args.args[0]
    assert sent["channel_id"] == "desktop"
    assert sent["payload"]["references"][0]["imageUrl"].endswith("logo.png")
    fallback = json.loads(result)["references"]
    assert fallback == [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]


def test_append_reference_filters_unsafe_urls_at_tool_boundary():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-desktop",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()

    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            return_value=True,
        ),
    ):
        result = asyncio.run(
            toolkit.append_reference(
                [
                    {
                        "title": "危险链接",
                        "url": "javascript:alert(1)",
                        "source": "web_search",
                        "name": "示例站",
                    },
                    {
                        "title": "安全链接",
                        "url": "https://example.com/safe",
                        "source": "web_search",
                        "name": "示例站",
                        "imageUrl": "data:image/svg+xml,<svg/>",
                    },
                ]
            )
        )

    payload = json.loads(result)
    assert payload["references"] == [
        {
            "title": "安全链接",
            "url": "https://example.com/safe",
            "source": "web_search",
            "name": "示例站",
        }
    ]
    sent = mock_server.send_push.await_args.args[0]
    assert sent["payload"]["references"] == payload["references"]


def test_append_reference_keeps_urls_when_push_fails():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-desktop",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(side_effect=RuntimeError("no connection"))
    refs = [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]

    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ),
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["history_saved"] is True
    assert payload["reference_event_sent"] is False
    assert "live delivery failed" in payload["message"]
    assert payload["error"] == "live push failed: no connection"
    assert payload["references"][0]["url"] == "https://example.com/a"


def test_append_reference_does_not_report_saved_when_history_is_skipped():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-default",
        channel_id="desktop",
    )
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock(side_effect=RuntimeError("no connection"))
    refs = [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]

    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            return_value=False,
        ),
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["history_saved"] is False
    assert payload["reference_event_sent"] is False
    assert "history write skipped" in payload["error"]
    assert "live push failed: no connection" in payload["error"]


def test_append_reference_rejects_empty():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="xiaoyi",
    )
    result = asyncio.run(toolkit.append_reference([]))
    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["count"] == 0
    assert payload["message"].startswith("Failed to send references")
    assert payload["references"] == []


def test_append_reference_returns_structured_error_when_delivery_fails():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="xiaoyi",
    )
    refs = [
        {
            "title": "标题",
            "url": "https://example.com/a",
            "source": "web_search",
            "name": "示例站",
        }
    ]
    mock_server = MagicMock()
    mock_server.send_push = AsyncMock()
    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.agents.harness.common.tools.xiaoyi_append_reference.resolve_toolkit_delivery",
            side_effect=RuntimeError("identity unavailable"),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
        ),
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    payload = json.loads(result)
    assert payload["success"] is False
    assert payload["history_saved"] is False
    assert payload["reference_event_sent"] is False
    assert payload["references"][0]["url"] == "https://example.com/a"
    assert payload["error"] == "delivery setup failed: identity unavailable"


def test_append_reference_still_pushes_when_history_write_fails():
    toolkit = XiaoyiAppendReferenceToolkit(
        request_id="r1",
        session_id="sess-1",
        channel_id="xiaoyi",
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
    with (
        _patch_agent_server(mock_server),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.append_history_record",
            side_effect=OSError("disk full"),
        ),
    ):
        result = asyncio.run(toolkit.append_reference(refs))

    payload = json.loads(result)
    assert payload["success"] is True
    assert payload["history_saved"] is False
    assert payload["reference_event_sent"] is True
    assert mock_server.send_push.await_count == 1
    assert "history write failed: disk full" in payload["error"]
