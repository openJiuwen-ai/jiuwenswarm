"""Unit coverage for the Jiuwen Celia Memory adapter."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from jiuwenswarm.agents.harness.common.memory.celia.client import CeliaMcpClient
from jiuwenswarm.agents.harness.common.memory.celia import rail as celia_rail_module
from jiuwenswarm.agents.harness.common.memory.celia.config import (
    CeliaConfig,
    CeliaEndpointConfig,
    build_celia_config,
)
from jiuwenswarm.agents.harness.common.memory.celia.provider import (
    CeliaMemoryProvider,
    _error_payload,
    _redact_diagnostic,
)
from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import read_memory_state
from jiuwenswarm.agents.harness.common.memory.celia.runtime_store import (
    CeliaRuntimeStore,
    get_runtime_store,
)
from jiuwenswarm.agents.harness.common.memory.celia.sanitizer import clean_turn_events
from jiuwenswarm.agents.harness.common.memory.external_memory_config import get_external_memory_config
from jiuwenswarm.agents.harness.common.memory.external_memory_builder import build_external_memory_rail


def _config() -> CeliaConfig:
    return CeliaConfig(
        server_binary_path="/opt/celia_memory_mcp_server",
        db_path="/tmp/celia.db",
        log_path="/tmp/celia.log",
        tenant_id="tenant-a",
        user_id="user-a",
        scope_id="user",
        embed=CeliaEndpointConfig(
            base_url="https://embed.example",
            api_key="embed-secret",
            model="embed-model",
            headers={"x-api-key": "header-secret", "x-request-from": "jiuwen"},
        ),
        chat=CeliaEndpointConfig(
            base_url="https://chat.example",
            api_key="chat-secret",
            model="chat-model",
        ),
    )


def test_child_env_keeps_dedicated_headers_out_of_extra_headers():
    env = _config().child_env({})
    assert env["OPENAI_EMBED_API_KEY"] == "embed-secret"
    assert env["OPENAI_EMBED_HEADERS_JSON"] == '{"x-request-from":"jiuwen"}'
    assert "x-api-key" not in env["OPENAI_EMBED_HEADERS_JSON"]


def test_external_builder_dispatches_celia_provider(monkeypatch):
    config = {
        "memory": {
            "engine": "external",
            "external": {"provider": "CELIA", "celia": {"tenant_id": "t"}},
        }
    }
    assert get_external_memory_config(config)["provider"] == "celia"
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.external_memory_builder._build_celia_rail",
        lambda config, ext_cfg, *, session_id, request_metadata: "celia-rail",
    )
    assert build_external_memory_rail(config, session_id="conversation-a") == "celia-rail"


@pytest.mark.parametrize("value, expected", [(None, False), (False, False), ("false", False), (True, True), ("true", True)])
def test_celia_preflight_config_is_opt_in(tmp_path, value, expected):
    section = {} if value is None else {"preflight_enabled": value}
    config = build_celia_config({}, {"celia": section}, workspace_dir=str(tmp_path))
    assert config.preflight_enabled is expected


@pytest.mark.asyncio
@pytest.mark.parametrize("enabled", [False, True])
async def test_provider_preflight_can_be_disabled(monkeypatch, enabled):
    preflight = Mock(return_value=["Celia requires Linux"])
    manager = SimpleNamespace(acquire=AsyncMock(side_effect=RuntimeError("backend unavailable")))
    monkeypatch.setattr(CeliaConfig, "preflight_issues", preflight)
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.celia.provider.get_celia_client_manager",
        lambda: manager,
    )
    provider = CeliaMemoryProvider(replace(_config(), preflight_enabled=enabled))
    with pytest.raises(Exception, match="Celia requires Linux" if enabled else "backend unavailable"):
        await provider.initialize()
    assert provider.is_initialized is False
    if enabled:
        preflight.assert_called_once()
        manager.acquire.assert_not_awaited()
    else:
        preflight.assert_not_called()
        manager.acquire.assert_awaited_once()


def test_runtime_state_is_fail_closed(tmp_path, monkeypatch):
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=1\n", encoding="utf-8")
    assert read_memory_state(str(runtime)) is True
    runtime.write_text("MEMORYSTATE=invalid\n", encoding="utf-8")
    assert read_memory_state(str(runtime)) is False
    monkeypatch.delenv("MEMORYSTATE", raising=False)
    assert read_memory_state(str(tmp_path / "missing")) is False


def test_runtime_store_limits_and_consumes_urgent():
    store = CeliaRuntimeStore()
    key = "tenant:user:conversation"
    for i in range(20):
        store.append_prompt(key, f"memory-{i}")
    assert len(store.prompt_values(key)) == 16
    store.mark_urgent(key)
    assert store.consume_urgent(key) is True
    assert store.consume_urgent(key) is False
    store.record_l1_paths(key, ["scene/a", "scene/b"])
    assert store.served_l1_paths(key) == ["scene/a", "scene/b"]


def test_rail_reads_typed_openjiuwen_model_response_and_sanitizes_ids():
    response = SimpleNamespace(
        content="assistant answer",
        reasoning_content="private reasoning",
        tool_calls=[
            {"id": "call-1", "name": "memory_record_search", "arguments": "{\"query\":\"x\"}"}
        ],
    )
    ctx = SimpleNamespace(inputs=SimpleNamespace(response=response))
    event = celia_rail_module.CeliaMemoryRail._model_event(ctx)
    assert event["text"] == "assistant answer"
    cleaned = clean_turn_events([event])
    assert cleaned[0]["toolCall"][0]["name"] == "memory_record_search"
    assert "id" not in cleaned[0]["toolCall"][0]


class _FakeClient:
    def __init__(self):
        self.calls = []
        self.restart_callbacks = []

    def add_restart_callback(self, callback):
        self.restart_callbacks.append(callback)

    async def call_tool(self, name, args, **kwargs):
        self.calls.append((name, args))
        await asyncio.sleep(0)
        if name == "memory_record_search":
            return {"results": [{"id": "m-1", "score": 0.95, "content": "remembered"}]}
        if name == "memory_add":
            return {"status": 0}
        if name == "memory_open":
            return {"status": 0}
        return {"status": 0}

    async def load_l1_batch(self, *args, **kwargs):
        self.calls.append(("memory_load_l1", {"paths": args[0] if args else []}))
        return {"entries": []}


class _FakeSessions:
    async def ensure_tool_session(self, user_id):
        return f"tools-{user_id}"


class _FailingSessions:
    async def ensure_tool_session(self, user_id):
        raise RuntimeError("memory_open failed")


@pytest.mark.asyncio
async def test_provider_initialization_uses_new_tool_discovery(monkeypatch):
    class Client:
        def __init__(self):
            self.calls = []

        async def list_tools(self):
            return {
                "memory_add", "memory_store", "memory_record_search",
                "memory_global_load", "memory_scene_load", "memory_scene_search",
            }

        async def call_tool(self, name, args, **kwargs):
            self.calls.append((name, args))
            return {"status": 0}

    class Sessions:
        async def ensure_tool_session(self, user_id):
            return f"tools-{user_id}"

    client = Client()
    lease = SimpleNamespace(client=client, sessions=Sessions())

    class Manager:
        async def acquire(self, config):
            return lease

        async def release(self, current_lease):
            return None

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.celia.provider.get_celia_client_manager",
        lambda: Manager(),
    )
    monkeypatch.setattr(CeliaConfig, "preflight_issues", lambda self: [])
    provider = CeliaMemoryProvider(_config(), user_id="alice")

    await provider.initialize()

    assert provider.is_initialized is True
    assert provider._supported_mcp_tools == await client.list_tools()
    assert client.calls == []

    await provider.sync_turn("user question", "assistant answer")
    assert client.calls[0][0] == "memory_add"

    await provider.shutdown()


@pytest.mark.asyncio
async def test_provider_calls_record_search_and_direct_memory_store(tmp_path):
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=true\n", encoding="utf-8")
    provider = CeliaMemoryProvider(
        replace(_config(), runtime_state_path=str(runtime)),
        user_id="alice", scope_id="user", session_id="conversation-a"
    )
    client = _FakeClient()
    provider._lease = SimpleNamespace(client=client, sessions=_FakeSessions())
    provider._initialized = True

    result = json.loads(await provider.handle_tool_call("memory_record_search", {"query": "where", "searchType": "atomic_fact"}))
    assert result["result"]["results"][0]["id"] == "m-1"
    assert client.calls[0][0] == "memory_record_search"
    assert client.calls[0][1]["userId"] == "alice"

    stored = json.loads(await provider.handle_tool_call("memory_store", {"content": "keep this"}))
    assert stored["ok"] is True
    assert client.calls[1][0] == "memory_store"
    await provider.sync_turn("user question", "assistant answer")
    add_call = next(args for name, args in client.calls if name == "memory_add")
    assert add_call["skipExtraction"] == 0
    assert add_call["userId"] == "alice"


@pytest.mark.asyncio
async def test_memory_store_requires_backend_persistence(tmp_path):
    get_runtime_store().clear_all()
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=true\n", encoding="utf-8")
    provider = CeliaMemoryProvider(
        replace(_config(), runtime_state_path=str(runtime)),
        user_id="alice", scope_id="user", session_id="conversation-a",
    )
    provider._lease = SimpleNamespace(sessions=_FailingSessions())
    result = json.loads(
        await provider.handle_tool_call(
            "memory_store", {"content": "I like traveling to the seaside"}
        )
    )

    assert result["ok"] is False
    context = provider._context({})
    assert get_runtime_store().prompt_values(context.store_key) == []


def test_provider_error_diagnostic_redacts_credentials():
    result = json.loads(_error_payload("memory_open", RuntimeError("api_key=secret-value")))

    assert result["error"] == "Celia memory operation failed"
    assert _redact_diagnostic("api_key=secret-value") == "api_key=<redacted>"
    assert _redact_diagnostic("authorization: bearer-value") == "authorization: <redacted>"


@pytest.mark.asyncio
async def test_provider_preserves_openclaw_memory_state_zero_write(tmp_path):
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=false\n", encoding="utf-8")
    provider = CeliaMemoryProvider(
        replace(_config(), runtime_state_path=str(runtime)),
        user_id="alice", scope_id="user", session_id="conversation-a"
    )
    client = _FakeClient()
    provider._lease = SimpleNamespace(client=client, sessions=_FakeSessions())
    provider._initialized = True

    disabled = await provider.handle_tool_call("memory_record_search", {"query": "where", "searchType": "atomic_fact"})
    assert "memory_disabled" in disabled
    stored = json.loads(await provider.handle_tool_call("memory_store", {"content": "keep this"}))
    assert stored["reason"] == "memory_disabled"
    await provider.sync_turn("user question", "assistant answer")
    add_call = next(args for name, args in client.calls if name == "memory_add")
    assert add_call["skipExtraction"] == 1
    assert len([call for call in client.calls if call[0] == "memory_add"]) == 2


@pytest.mark.asyncio
async def test_client_decodes_double_encoded_tool_payload(monkeypatch):
    client = CeliaMcpClient(_config())

    async def fake_start():
        return None

    async def fake_request(method, params, *, timeout, generation=None):
        assert method == "tools/call"
        return {"content": [{"text": json.dumps(json.dumps({"status": 0}))}]}

    monkeypatch.setattr(client, "start", fake_start)
    monkeypatch.setattr(client, "_request", fake_request)
    assert await client.call_tool("memory_global_load", {"userId": "alice"}) == {"status": 0}


@pytest.mark.asyncio
async def test_rail_captures_context_messages_without_final_result():
    calls = []

    class Provider:
        name = "celia"
        config = SimpleNamespace(request_timeout=1.0, normalized_db_path="/tmp/celia.db")

        async def sync_turn(self, user_msg, assistant_msg, **kwargs):
            calls.append((user_msg, assistant_msg, kwargs))

        async def report_round_usage(self, **kwargs):
            return None

    rail = celia_rail_module.CeliaMemoryRail(
        Provider(), user_id="alice", session_id="conversation-a"
    )
    rail._initialized = True
    ctx = SimpleNamespace(
        inputs=SimpleNamespace(
            query="",
            result=None,
            messages=[
                {"role": "system", "content": "startup"},
                {"role": "user", "content": "记一下我喜欢去海边旅游"},
                {
                    "role": "assistant",
                    "content": "Noted",
                    "tool_calls": [
                        {"id": "call-1", "name": "memory_store", "arguments": "{}"}
                    ],
                },
            ],
        )
    )

    await rail.after_invoke(ctx)

    assert len(calls) == 1
    user_msg, assistant_msg, kwargs = calls[0]
    assert user_msg == "记一下我喜欢去海边旅游"
    assert assistant_msg == "Noted"
    assert kwargs["events"][0]["role"] == "user"
    assert kwargs["events"][1]["role"] == "assistant"
