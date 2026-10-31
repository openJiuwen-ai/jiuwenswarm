# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""runtime 客户端按专用网址发请求。没有网址的方法直接报还没有 HTTP 处理。"""

from __future__ import annotations

import sys
import types
from pathlib import Path

import pytest

_KV_CACHE_MOD = "openjiuwen.core.kv_cache"
if _KV_CACHE_MOD not in sys.modules:
    try:
        __import__(_KV_CACHE_MOD)
    except ModuleNotFoundError:
        _stub = types.ModuleType(_KV_CACHE_MOD)
        _stub.KVCacheAffinityConfig = type("KVCacheAffinityConfig", (), {})
        sys.modules[_KV_CACHE_MOD] = _stub

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.extensions.runtime_management_extension.pod_rest_map import (
    HttpMethodNotHandled,
    assemble_rest_request,
)


def _envelope(method: str, **kwargs) -> E2AEnvelope:
    return E2AEnvelope(
        request_id="req-1",
        channel="web",
        method=method,
        session_id="sess-1",
        params={"session_id": "sess-1", "title": "你好"},
        **kwargs,
    )


def test_chat_uses_the_dedicated_urls() -> None:
    sent = assemble_rest_request(
        _envelope("chat.send", is_stream=True),
        base_url="http://10.0.0.8:8766",
    )
    resumed = assemble_rest_request(
        _envelope("chat.resume"),
        base_url="http://10.0.0.8:8766",
    )
    assert sent.verb == "POST"
    assert sent.url == "http://10.0.0.8:8766/api/v1/chat/completions"
    assert sent.used_rpc_fallback is False
    assert sent.headers["Accept"] == "text/event-stream"
    assert resumed.url == "http://10.0.0.8:8766/api/v1/chat/resume"
    assert resumed.used_rpc_fallback is False


def test_session_and_agent_methods_use_dedicated_urls() -> None:
    created = assemble_rest_request(
        _envelope("session.create"),
        base_url="http://10.0.0.8:8766",
    )
    listed = assemble_rest_request(
        _envelope("agents.list"),
        base_url="http://10.0.0.8:8766/api/v1",
    )
    history = assemble_rest_request(
        _envelope("history.get", is_stream=True),
        base_url="http://10.0.0.8:8766",
    )
    removed = assemble_rest_request(
        _envelope("session.delete"),
        base_url="http://10.0.0.8:8766",
    )
    assert created.verb == "POST"
    assert created.url == "http://10.0.0.8:8766/api/v1/sessions"
    assert created.used_rpc_fallback is False
    assert "session_id" not in (created.json_body or {})
    assert created.json_body == {"title": "你好"}
    assert listed.verb == "GET"
    assert listed.url == "http://10.0.0.8:8766/api/v1/agents"
    assert listed.used_rpc_fallback is False
    assert history.url == "http://10.0.0.8:8766/api/v1/sessions/sess-1/history/stream"
    assert history.headers["Accept"] == "text/event-stream"
    assert removed.verb == "DELETE"
    assert removed.url == "http://10.0.0.8:8766/api/v1/sessions/sess-1"


def test_session_upload_reader_has_dedicated_route_and_identity_headers() -> None:
    envelope = _envelope("file.read_upload_chunk", user_id="u", channel_context={
        "routing": {"group_id": "g", "bot_id": "b"},
    })
    envelope.params = {"path": "/tenant/agent/sessions/sess-1/uploads/photo.png", "offset": 2, "limit": 512}
    request = assemble_rest_request(envelope, base_url="http://10.0.0.8:8766")
    assert request.verb == "GET"
    assert request.url == "http://10.0.0.8:8766/api/v1/sessions/sess-1/files/upload-content"
    assert request.headers["X-User-Id"] == "u"
    assert request.headers["X-Group-Id"] == "g" and request.headers["X-Bot-Id"] == "b"
    assert request.query["path"] == envelope.params["path"]


def test_forwarded_methods_with_handlers_get_dedicated_urls() -> None:
    expected = {
        "mcp.list": "/api/v1/mcp/actions/list",
        "mcp.show": "/api/v1/mcp/actions/show",
        "mcp.install": "/api/v1/mcp/actions/install",
        "mcp.uninstall": "/api/v1/mcp/actions/uninstall",
        "mcp.connect": "/api/v1/mcp/actions/connect",
        "mcp.wait_auth": "/api/v1/mcp/actions/wait-auth",
        "mcp.cancel_connect": "/api/v1/mcp/actions/cancel-connect",
        "mcp.disconnect": "/api/v1/mcp/actions/disconnect",
        "mcp.register_custom": "/api/v1/mcp/actions/register-custom",
        "mcp.delete_custom": "/api/v1/mcp/actions/delete-custom",
        "mcp.save_credentials": "/api/v1/mcp/actions/save-credentials",
        "swarmflow.pause": "/api/v1/sessions/sess-1/swarmflow/actions/pause",
        "swarmflow.resume": "/api/v1/sessions/sess-1/swarmflow/actions/resume",
        "swarmflow.stop": "/api/v1/sessions/sess-1/swarmflow/actions/stop",
        "proactive.feedback": "/api/v1/proactive/actions/feedback",
    }
    for method, suffix in expected.items():
        sent = assemble_rest_request(
            _envelope(method),
            base_url="http://10.0.0.8:8766",
        )
        assert sent.verb == "POST"
        assert sent.url == "http://10.0.0.8:8766" + suffix
        assert sent.used_rpc_fallback is False


def test_agent_templates_list_uses_the_dedicated_url() -> None:
    sent = assemble_rest_request(
        _envelope("agent_templates.list"),
        base_url="http://10.0.0.8:8766",
    )
    assert sent.verb == "GET"
    assert sent.url == "http://10.0.0.8:8766/api/v1/agent-templates"
    assert sent.used_rpc_fallback is False
    assert sent.query is not None
    assert sent.query["session_id"] == "sess-1"


def test_skills_list_uses_the_dedicated_url() -> None:
    sent = assemble_rest_request(
        _envelope("skills.list"),
        base_url="http://10.0.0.8:8766",
    )
    assert sent.verb == "GET"
    assert sent.url == "http://10.0.0.8:8766/api/v1/skills"
    assert sent.used_rpc_fallback is False
    assert sent.query is not None
    assert sent.query["session_id"] == "sess-1"


def test_methods_without_handlers_are_not_sent_anywhere() -> None:
    for method in (
        "plugins.list",
        "channel.feishu.get_conf",
        "updater.check",
        "heartbeat.get_conf",
    ):
        with pytest.raises(HttpMethodNotHandled, match="还没有 HTTP 处理"):
            assemble_rest_request(
                _envelope(method),
                base_url="http://10.0.0.8:8766",
            )


def test_project_cron_binding_and_lifecycle_use_dedicated_urls() -> None:
    binding = assemble_rest_request(
        E2AEnvelope(
            request_id="req-1",
            channel="web",
            method="project.cron.resolve_binding",
            params={"project_id": "proj_1", "project_dir": "/work/zxy"},
        ),
        base_url="http://10.0.0.8:8766",
    )
    lifecycle = assemble_rest_request(
        E2AEnvelope(
            request_id="req-1",
            channel="web",
            method="project.lifecycle",
            params={"project_id": "proj_1"},
        ),
        base_url="http://10.0.0.8:8766",
    )
    assert binding.verb == "POST"
    assert binding.url == "http://10.0.0.8:8766/api/v1/projects/actions/resolve-cron-binding"
    assert binding.used_rpc_fallback is False
    assert lifecycle.verb == "POST"
    assert lifecycle.url == "http://10.0.0.8:8766/api/v1/projects/proj_1/lifecycle"
    dispatch = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server" / "dispatch.py"
    text = dispatch.read_text(encoding="utf-8")
    assert "ReqMethod.PROJECT_CRON_RESOLVE_BINDING: handle_gateway_adapter" in text
    assert "ReqMethod.PROJECT_LIFECYCLE: handle_gateway_adapter" in text


def test_session_pin_uses_the_dedicated_url() -> None:
    sent = assemble_rest_request(
        E2AEnvelope(
            request_id="req-1",
            channel="web",
            method="session.pin",
            session_id="sess-1",
            params={"session_id": "sess-1", "pinned": True},
        ),
        base_url="http://10.0.0.8:8766",
    )
    assert sent.verb == "POST"
    assert sent.url == "http://10.0.0.8:8766/api/v1/sessions/sess-1/actions/pin"
    assert sent.used_rpc_fallback is False
    assert sent.json_body == {"pinned": True}
    dispatch = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server" / "dispatch.py"
    text = dispatch.read_text(encoding="utf-8")
    assert "ReqMethod.SESSION_PIN: handle_gateway_adapter" in text


def test_session_delete_is_registered_for_rpc() -> None:
    dispatch = Path(__file__).resolve().parents[3] / "jiuwenswarm" / "server" / "dispatch.py"
    text = dispatch.read_text(encoding="utf-8")
    assert "ReqMethod.SESSION_DELETE: handle_session_delete" in text
