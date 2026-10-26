"""Tests for bounded inbound payload logging."""

from __future__ import annotations

import json
from unittest.mock import patch

from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.wire_parse import parse_inbound


def test_catalog_sync_logs_summary_without_changing_parsed_request() -> None:
    large_marker = "catalog-secret-value-" * 10_000
    payload = {
        "request_id": "sync-1",
        "channel_id": "officeclaw",
        "req_method": ReqMethod.SYNC_AGENTS_CONFIGS.value,
        "params": {
            "revision": "revision-1",
            "service_id": "default",
            "agents": [
                {"agent_id": "office", "config": {"marker": large_marker}},
                {"agent_id": "assistant", "config": {}},
            ],
        },
    }
    raw = json.dumps(payload)

    with patch("jiuwenswarm.server.wire_parse.logger.info") as log_info:
        result = parse_inbound(raw)

    assert result.ok
    assert result.request is not None
    assert result.request.request_id == payload["request_id"]
    assert result.request.req_method is ReqMethod.SYNC_AGENTS_CONFIGS
    assert result.request.params == payload["params"]

    first_log = log_info.call_args_list[0]
    rendered_call = repr(first_log)
    assert "Inbound raw payload: <summary>" in first_log.args[0]
    assert "agent_count=%s" in first_log.args[0]
    assert 2 in first_log.args
    assert large_marker not in rendered_call


def test_chat_payload_keeps_existing_masked_diagnostic_log() -> None:
    payload = {
        "request_id": "chat-1",
        "channel_id": "officeclaw",
        "req_method": ReqMethod.CHAT_SEND.value,
        "params": {"query": "private chat text"},
    }

    with patch("jiuwenswarm.server.wire_parse.logger.info") as log_info:
        result = parse_inbound(json.dumps(payload))

    assert result.ok
    first_log = log_info.call_args_list[0]
    assert first_log.args[0] == "[AgentWebSocketServer] Inbound raw payload: %s"
    assert first_log.args[1]["params"]["query"] == "******"
