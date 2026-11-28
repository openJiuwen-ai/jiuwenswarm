import asyncio
import json
import sqlite3
import sys
from concurrent.futures import ThreadPoolExecutor, wait
from contextlib import closing
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.util import find_spec
from pathlib import Path
from threading import Event, Thread
from types import SimpleNamespace
from unittest.mock import AsyncMock
from urllib.parse import parse_qs, urlsplit

import httpx
import pytest

from openjiuwen.core.foundation.llm import AssistantMessage, Model, ModelClientConfig, ModelRequestConfig
from openjiuwen.core.runner import Runner
from openjiuwen.harness import create_deep_agent

from jiuwenswarm.agents.harness.common.memory import plugin
from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import build_jiuwen_memory_rail
from jiuwenswarm.agents.harness.common.memory.team_memory_members import (
    prepare_team_memory,
    revoke_expert_group_memory,
    revoke_expert_template_memory,
    supports_team_memory_membership,
)
from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.common import paths
from jiuwenswarm.common.team_memory_binding import team_memory_key
from jiuwenswarm.server import agent_ws_server
from jiuwenswarm.server.runtime import extension_package_manager as packages
from jiuwenswarm.server.runtime import managed_memory_http as managed
from jiuwenswarm.server.runtime.agent_adapter import interface_code, interface_deep
from jiuwenswarm.server.runtime.memory_portal import get_memory_portal_service
from jiuwenswarm.server.runtime.session.session_metadata import get_session_equipment


def test_optional_plugin_package_config_requires_mapping(tmp_path):
    assert packages.read_plugin_package_config(tmp_path) == {}
    path = tmp_path / "config.yaml"
    path.write_text("identity_user: local-tester\n", encoding="utf-8")
    assert packages.read_plugin_package_config(tmp_path) == {"identity_user": "local-tester"}
    path.write_text("- invalid\n", encoding="utf-8")
    with pytest.raises(ValueError, match="Plugin configuration must be a mapping"):
        packages.read_plugin_package_config(tmp_path)


@pytest.fixture
def memory_service():
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            requests.append((self.path, body))
            replies = {
                "/v1/search": {"items": [{"unit_id": "test-unit", "content": "violet-otter-713", "tier": "semantic"}]},
                "/v1/add": {"units": [{"id": "saved-turn"}]},
            }
            data = json.dumps(replies.get(self.path, {})).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", requests
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


@pytest.mark.asyncio
@pytest.mark.parametrize("mode,adapter_type", [
    ("agent", interface_deep.JiuWenSwarmDeepAdapter),
    ("code", interface_code.JiuwenSwarmCodeAdapter),
])
async def test_bundled_plugin_recall_writeback_and_explicit_disable(
    mode, adapter_type, tmp_path, monkeypatch, memory_service,
):
    url, requests = memory_service
    marker = "violet-otter-713"
    options = {"mode": "sdk", "base_url": "http://127.0.0.1:1"}
    monkeypatch.setattr(managed.managed_memory_http, "connection_options", lambda: {"base_url": url})
    memory_running = True
    monkeypatch.setattr(managed.managed_memory_http, "status", lambda: {"running": memory_running})
    config = {
        "react": {},
        "modes": {"agent": {"memory": {"enabled": True}}, "code": {"memory": {"enabled": True}}},
        "memory": {"engine": "both", "mode": "local", "external": {"provider": "jiuwen"}, "builtin": {
            "provider": "jiuwen", "jiuwen": options,
        }},
    }
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    for module in (plugin, interface_deep, interface_code):
        monkeypatch.setattr(module, "get_config", lambda: config)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *args, **kwargs: None)
    plugin.ensure_memory_plugin_installed()
    installed = packages.resolve_plugin_dir(plugin.MEMORY_PLUGIN_ID)
    assert installed.parent.name == "built_in"
    assert installed.samefile(paths.get_agent_workspace_dir() / "plugins/plugin_packages/built_in" / plugin.MEMORY_PLUGIN_ID)
    seen_messages = []

    async def fake_model(self, messages, **kwargs):
        seen_messages.append("\n".join(str(message.content) for message in messages))
        return AssistantMessage(content="A deterministic reply.")

    monkeypatch.setattr(Model, "invoke", fake_model)
    model = Model(model_client_config=ModelClientConfig(
        client_provider="OpenAI", api_key="test", api_base="http://127.0.0.1:1",
    ), model_config=ModelRequestConfig(model="memory-plugin-test"))
    adapter = adapter_type()
    session_id = f"memory-test-{mode}"
    project_dir = tmp_path / "workspace"
    project_dir.mkdir()
    adapter._parent_session_id = session_id
    adapter._agent_workspace_dir = str(project_dir)
    adapter._workspace_dir = str(project_dir)
    adapter._project_dir = str(project_dir)
    adapter._config_base_cache = config
    adapter._config_cache = config["react"]
    adapter._model, adapter._default_model_name = model, "memory-plugin-test"
    adapter._instance = create_deep_agent(
        model=model, workspace=str(project_dir), auto_create_workspace=False,
        enable_task_loop=False, add_general_purpose_agent=False, enable_read_image_multimodal=False,
    )

    async def select(*, rejected=False, **params):
        response = await adapter._ensure_chat_extensions(SimpleNamespace(
            params={"mode": mode, **params}, session_id=session_id,
            request_id="memory-test", channel_id="web", metadata={},
        ))
        if rejected:
            assert response is not None
            assert "equipment change rejected at non-fresh turn" in str(response.payload)
        else:
            assert response is None, getattr(response, "payload", None)

    coding_scene = mode == "code" and find_spec("jiuwen_memory_adapter.jiuwenswarm.coding_memory") is not None
    tool = "agent_memory_coding_memory_search" if coding_scene else "agent_memory_search"

    await Runner.start()
    try:
        await adapter._instance.ensure_initialized()
        await adapter._handle_memory_rail_by_config(mode)
        await adapter._handle_external_memory_rail_by_config()
        assert adapter._memory_rail is None
        assert adapter._external_memory_rail is None
        assert getattr(adapter, "_coding_memory_rail", None) is None
        await select()
        assert get_session_equipment(session_id)["plugin_names"] == [plugin.MEMORY_PLUGIN_ID]
        binding = adapter.get_plugin_runtime_manager().bindings[plugin.MEMORY_PLUGIN_ID]
        assert len(binding.runtime.resources) == 1
        rail = binding.runtime.resources[0]
        assert adapter._instance.find_rail_by_name(type(rail).__name__) is rail
        assert adapter._instance.ability_manager.get(tool) is not None

        query = "What should we remember?"
        await adapter._instance.invoke({"query": query, "conversation_id": session_id})
        assert marker in seen_messages[-1]
        recalled_count = seen_messages[-1].count(marker)
        await select(plugin_names=[])
        assert get_session_equipment(session_id)["plugin_names"] == []
        assert adapter._instance.find_rail_by_name(type(rail).__name__) is None
        assert adapter._instance.ability_manager.get(tool) is None
        writes = [body for path, body in requests if path == "/v1/add"]
        assert len(writes) == 1
        assert query in json.dumps(writes[0])

        previous_request_count = len(requests)
        await select()
        await adapter._handle_memory_rail_by_config(mode)
        await adapter._handle_external_memory_rail_by_config()
        assert adapter.get_plugin_runtime_manager().bindings == {}
        assert adapter._memory_rail is None
        assert adapter._external_memory_rail is None
        assert getattr(adapter, "_coding_memory_rail", None) is None
        await adapter._instance.invoke({"query": "New turn after disabling memory", "conversation_id": session_id})
        await asyncio.sleep(0)
        assert len(requests) == previous_request_count
        assert seen_messages[-1].count(marker) == recalled_count
        await adapter._instance.invoke({"query": "New conversation", "conversation_id": f"{session_id}-fresh"})
        assert marker not in seen_messages[-1]
        assert len(requests) == previous_request_count
        memory_running = False
        await select(attach_goal=True, plugin_names=[plugin.MEMORY_PLUGIN_ID], rejected=True)
        assert get_session_equipment(session_id)["plugin_names"] == []
        await select(plugin_names=[plugin.MEMORY_PLUGIN_ID])
        assert adapter.get_plugin_runtime_manager().bindings == {}
        assert get_session_equipment(session_id)["plugin_names"] == [plugin.MEMORY_PLUGIN_ID]
        await select(attach_goal=True, plugin_names=[], rejected=True)
        assert get_session_equipment(session_id)["plugin_names"] == [plugin.MEMORY_PLUGIN_ID]
        await adapter._instance.invoke({"query": "Continue without memory", "conversation_id": session_id})
        assert "Continue without memory" in seen_messages[-1]
        assert len(requests) == previous_request_count

        memory_running = True
        await select(attach_goal=True, plugin_names=[plugin.MEMORY_PLUGIN_ID])
        assert adapter.get_plugin_runtime_manager().bindings == {}
        assert get_session_equipment(session_id)["plugin_names"] == [plugin.MEMORY_PLUGIN_ID]
        await select()
        assert adapter._instance.ability_manager.get(tool) is not None
        binding = adapter.get_plugin_runtime_manager().bindings[plugin.MEMORY_PLUGIN_ID]
        memory_running = False
        monkeypatch.setattr(managed.managed_memory_http, "connection_options", managed.ManagedMemoryHttp().connection_options)
        await select(attach_goal=True)
        assert adapter.get_plugin_runtime_manager().bindings[plugin.MEMORY_PLUGIN_ID] is binding
    finally:
        await adapter.get_plugin_runtime_manager().close()
        await Runner.stop()

@pytest.mark.asyncio
async def test_bundled_plugin_writes_and_recalls_across_active_sessions(tmp_path, monkeypatch):
    import os
    import re
    import sqlite3
    from contextlib import ExitStack, closing

    from jiuwen_memory.common.llm.llm_impl.openai_llm import OpenAILLM
    from jiuwen_memory_adapter.jiuwenswarm.agent_memory_provider import _agent_default_config

    profile_marker = "豆汁儿"
    daily_marker = "SEG"
    calls = []
    recalling = False

    def extraction_reply(self, messages, **kwargs):
        if recalling:
            return "[]"
        prompt = "\n".join(message.content for message in messages if message.role == "user")
        marker = min((value for value in (profile_marker, daily_marker) if value in prompt), key=prompt.index)
        source_id = re.search(r"\[ID:\s*([^\]]+)\]", prompt).group(1)
        is_daily = marker == daily_marker
        calls.append(marker)
        return json.dumps([{
            "source_id": source_id, "target": "event" if is_daily else "preference",
            "memory_class": "daily_memory" if is_daily else "user_profile", "project_fact": False,
            "tier": "episodic" if is_daily else "semantic", "confidence": 0.98,
            "content": "用户明天下午要上SEG评审会。" if is_daily else "用户的工作地点是北京，喜欢喝豆汁儿。",
            "title": "Scheduled review" if is_daily else "Tea preference", "tags": ["reminder" if is_daily else "tea"],
        }])

    def set_up_adapter(config, session_id):
        model = Model(model_client_config=ModelClientConfig(
            client_provider="OpenAI", api_key="test", api_base="http://127.0.0.1:1",
        ), model_config=ModelRequestConfig(model="memory-document-test"))
        adapter = interface_deep.JiuWenSwarmDeepAdapter()
        adapter._parent_session_id = session_id
        adapter._agent_workspace_dir = str(tmp_path)
        adapter._workspace_dir = str(tmp_path)
        adapter._project_dir = str(tmp_path)
        adapter._config_base_cache = config
        adapter._config_cache = config["react"]
        adapter._model, adapter._default_model_name = model, "memory-document-test"
        adapter._instance = create_deep_agent(
            model=model, workspace=str(tmp_path), auto_create_workspace=False,
            enable_task_loop=False, add_general_purpose_agent=False, enable_read_image_multimodal=False,
        )
        return adapter

    from openjiuwen.core.foundation.llm import ToolCall

    questions = {
        "我的工作地点是什么？": ("agent_memory_read_md_file", "北京"),
        "我喜欢喝什么？": ("agent_memory_read_md_file", "豆汁儿"),
        "SEG评审会": ("agent_memory_search", "SEG"),
    }
    tool_results = []

    async def chat_reply(self, messages, **kwargs):
        last = messages[-1]
        if last.role == "tool":
            tool_results.append(last.content)
        elif last.role == "user" and last.content in questions:
            tool_name, _ = questions[last.content]
            args = {"query": last.content} if tool_name == "agent_memory_search" else {"md_filename": "USER.md"}
            return AssistantMessage(content="", tool_calls=[ToolCall(
                id=f"memory-recall-{len(tool_results)}", type="function", name=tool_name,
                arguments=json.dumps(args),
            )])
        return AssistantMessage(content="Understood.")

    monkeypatch.setattr(Model, "invoke", chat_reply)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *args, **kwargs: None)
    model_url = os.getenv("JW_TEST_MEMORY_LLM_URL")
    if not model_url:
        monkeypatch.setattr(OpenAILLM, "chat", extraction_reply)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    root = paths.get_agent_root_dir() / "memory" / "managed-http"
    sdk = _agent_default_config()
    sdk["globals"].update(
        embedder_dim=64, llm_base_url=model_url or "http://127.0.0.1:1",
        llm_model=os.getenv("JW_TEST_MEMORY_LLM_MODEL", "memory-document-test"),
        llm_api_key=os.getenv("JW_TEST_MEMORY_LLM_KEY", "test"),
    )
    sdk["embedder"] = {"default": {"target": "hashing", "params": {"tokenizer": "default"}}}
    options = {"mode": "sdk", "api_key": "", "sdk": sdk, "portal": {"enabled": True}}
    cfg = {"react": {}, "memory": {"builtin": {"provider": "jiuwen", "jiuwen": options}}}
    for module in (plugin, interface_deep, managed):
        monkeypatch.setattr(module, "get_config", lambda: cfg)
    plugin.ensure_memory_plugin_installed()

    with ExitStack() as cleanup:
        cleanup.callback(managed.managed_memory_http.stop)
        await asyncio.to_thread(managed.start_bundled_memory_http)
        adapters = []

        async def open_session(session_id):
            adapter = set_up_adapter(cfg, session_id)
            adapters.append(adapter)
            await adapter._instance.ensure_initialized()
            response = await adapter._ensure_chat_extensions(SimpleNamespace(
                params={"mode": "agent", "plugin_names": [plugin.MEMORY_PLUGIN_ID]},
                session_id=session_id, request_id=session_id, channel_id="web", metadata={},
            ))
            assert response is None, getattr(response, "payload", None)
            return adapter

        await Runner.start()
        try:
            for session_id, query in (
                ("profile-turn", "我的工作地点是北京，我爱喝豆汁儿"),
                ("daily-turn", "请记住：我明天下午要上SEG评审会"),
            ):
                writer = await open_session(session_id)
                await writer._instance.invoke({"query": query, "conversation_id": session_id})
                binding = writer.get_plugin_runtime_manager().bindings[plugin.MEMORY_PLUGIN_ID]
                write_task = binding.runtime.resources[0]._sync_task
                assert write_task is not None
                await write_task

            if not model_url:
                assert calls == [profile_marker, daily_marker]
            profile = (root / "USER.md").read_text(encoding="utf-8")
            assert "北京" in profile and profile_marker in profile
            assert any(daily_marker in path.read_text(encoding="utf-8") for path in root.glob("memory/**/daily_memory/*.md"))
            db_uri = (root / "memory/.shadow/shadow.db").as_uri() + "?mode=ro"
            with closing(sqlite3.connect(db_uri, uri=True)) as db:
                for marker in ("北京", profile_marker, daily_marker):
                    assert db.execute(
                        "SELECT 1 FROM memory_unit WHERE instr(unit_json, ?) > 0 LIMIT 1", (marker,),
                    ).fetchone() is not None

            portal = get_memory_portal_service()
            try:
                opened = await portal.open(cfg)
                if managed.supports_managed_memory_portal():
                    await _assert_managed_portal_lists_memories(opened, profile_marker, daily_marker)
                else:
                    assert opened["status"] == "unavailable"
                    assert opened["code"] == "dependency_unavailable"
            finally:
                await portal.close()

            recalling = True
            reader = await open_session("fresh-recall")
            for question, (tool_name, expected) in questions.items():
                assert reader._instance.ability_manager.get(tool_name) is not None
                previous_result_count = len(tool_results)
                await reader._instance.invoke({"query": question, "conversation_id": "fresh-recall"})
                assert len(tool_results) > previous_result_count
                assert expected in tool_results[-1], tool_results[-1]

        finally:
            for adapter in reversed(adapters):
                await adapter.get_plugin_runtime_manager().close()
            await Runner.stop()


async def _assert_managed_portal_lists_memories(opened, profile_marker, daily_marker):
    assert opened["status"] == "ready", opened
    assert opened["mode"] == "http"
    connection = managed.managed_memory_http.connection_options()
    address = connection["base_url"]
    assert opened["url"].startswith(f"{address}/portal/#access=")
    assert connection["api_key"] not in json.dumps(opened)
    token = parse_qs(urlsplit(opened["url"]).fragment)["access"][0]
    async with httpx.AsyncClient(base_url=address, trust_env=False) as browser:
        assert (await browser.get("/portal/")).status_code == 200
        browser.headers.update({"Origin": address, "X-Memory-Portal-Browser": "1"})
        bootstrap = await browser.post("/portal/api/bootstrap", json={"token": token, "embedded": True})
        bootstrap.raise_for_status()
        browser.headers["X-Memory-Portal-Session"] = bootstrap.json()["session_token"]
        page = await browser.post("/v1/list", json={"extensions": {"personal": {}}})
        page.raise_for_status()
        listed = json.dumps(page.json()["items"], ensure_ascii=False)
        assert profile_marker in listed
        assert daily_marker in listed


@pytest.mark.parametrize("llm_config", ["echo", {"target": "echo"}])
def test_shared_sdk_rail_skips_invalid_extraction_model(llm_config, caplog):
    from jiuwenswarm.agents.harness.common.memory.jiuwen_rail_builder import build_jiuwen_memory_rail

    config = {"memory": {"builtin": {"jiuwen": {
        "mode": "sdk", "sdk": {"llm": {"default": llm_config}},
    }}}}
    assert build_jiuwen_memory_rail(config, shared_document_root=True) is None
    assert "SDK extraction requires a real model" in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["team.code", "team.work"])
async def test_team_memory_plugin_mounts_and_detaches_shared_rail(mode, tmp_path, monkeypatch):
    from unittest.mock import AsyncMock

    from openjiuwen.harness.rails import JiuwenTeamMemoryRail

    from jiuwenswarm.agents.swarm.context import SwarmBuildContext
    from jiuwenswarm.agents.swarm.providers import team_memory
    from jiuwenswarm.common.team_memory_binding import team_memory_user_context

    config = {"memory": {"builtin": {"provider": "jiuwen"}}, "plugins": {
        plugin.MEMORY_PLUGIN_ID: {"mode": "sdk", "base_url": "https://unused.example.test"},
    }}
    monkeypatch.setattr(managed.managed_memory_http, "connection_options", lambda: {"base_url": "http://127.0.0.1:1"})
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    monkeypatch.setattr(plugin, "get_config", lambda: config)
    monkeypatch.setattr("openjiuwen.harness.deep_agent.schedule_image_support_probe", lambda *args, **kwargs: None)
    plugin.ensure_memory_plugin_installed()
    assert packages.resolve_plugin_dir(plugin.MEMORY_PLUGIN_ID).parent.name == "built_in"
    assert packages.resolve_plugin_runtime_dir(plugin.MEMORY_PLUGIN_ID).parent.name == "plugin_packages"
    session = "team-plugin-test"
    context = SwarmBuildContext(session_id=session, config=config, mode=mode, member_name="leader",
                                memory_team="team-key", memory_project="project-key")
    _, mount = team_memory.build_team_memory({}, context)
    assert context.extras["_coding_memory_rail"] is mount
    original_build = team_memory._build_plugin_rail
    created = []

    def build(*args):
        rail = original_build(*args)
        rail._provider.initialize = AsyncMock()
        rail._provider.prefetch = AsyncMock(return_value="the remembered team fact")
        rail._provider.sync_turn = AsyncMock()
        rail._provider.shutdown = AsyncMock()
        created.append(rail)
        return rail

    monkeypatch.setattr(team_memory, "_build_plugin_rail", build)
    prompts = []

    async def fake_model(self, messages, **kwargs):
        prompts.append("\n".join(str(message.content) for message in messages))
        return AssistantMessage(content="Deterministic reply.")

    monkeypatch.setattr(Model, "invoke", fake_model)
    model = Model(model_client_config=ModelClientConfig(
        client_provider="OpenAI", api_key="test", api_base="http://127.0.0.1:1",
    ), model_config=ModelRequestConfig(model="team-plugin-test"))
    search_tool = "team_memory_search" if supports_team_memory_membership() else "agent_memory_search"
    agents = []
    await Runner.start()
    try:
        for name in ("leader", "embedded-code-agent"):
            workspace = tmp_path / name
            workspace.mkdir()
            agent = create_deep_agent(
                model=model, workspace=str(workspace), rails=[mount], auto_create_workspace=False,
                enable_task_loop=False, add_general_purpose_agent=False, enable_read_image_multimodal=False,
            )
            agents.append(agent)
            query = "What did the team decide?"
            await agent.invoke({"query": query, "conversation_id": name,
                                "run": {"context": team_memory_user_context(None, query)}})
            assert "the remembered team fact" in prompts[-1]
            assert agent.ability_manager.get(search_tool) is not None
        leader, code = agents
        rail, = created
        assert isinstance(rail, JiuwenTeamMemoryRail)
        assert set(rail.attached_agents) == {leader, code}
        assert leader.find_rail_by_name(type(rail).__name__) is rail
        await rail.drain()
        assert rail._provider.sync_turn.await_count == 2
        assert all(call.kwargs["sources"] for call in rail._provider.sync_turn.await_args_list)

        plugin.persist_team_memory_selection(session, {"mode": mode, "plugin_names": []})
        await leader.invoke({"query": "Memory is now disabled", "conversation_id": "off"})
        assert "the remembered team fact" not in prompts[-1]
        assert get_session_equipment(session)["plugin_names"] == []
        assert all(agent.ability_manager.get(search_tool) is None for agent in agents)
        assert all(agent.find_rail_by_name(type(rail).__name__) is None for agent in agents)
        assert rail._provider.sync_turn.await_count == 2
        rail._provider.shutdown.assert_awaited_once()

        plugin.persist_team_memory_selection(session, {"mode": mode, "plugin_names": [plugin.MEMORY_PLUGIN_ID]})
        await leader.invoke({"query": "Memory is enabled again", "conversation_id": "on"})
        assert len(created) == 2
        assert "the remembered team fact" in prompts[-1]
    finally:
        for rail in created:
            await rail.aclose()
        await Runner.stop()


def test_managed_http_requires_key_and_stops(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    manager = managed.ManagedMemoryHttp()
    options = {"sdk": {"embedder": {"default": "hashing"}, "globals": {"embedder_dim": 64}}}
    started = manager.start({}, options)
    try:
        assert manager.start({}, options) == started
        address, key = started["base_url"], started["api_key"]
        assert address.startswith("http://127.0.0.1:")
        assert manager.status() == {"running": True, "base_url": address}
        with httpx.Client(trust_env=False, timeout=5) as client:
            body = {"scope": {"org": "local", "user": "developer"}, "limit": 1}
            assert client.post(f"{address}/v1/list", json=body).status_code == 401
            assert client.post(f"{address}/v1/list", json=body, headers={"Authorization": "Bearer wrong"}).status_code == 401
            launch = f"{address}/portal/api/launch"
            expected_status = 401 if managed.supports_managed_memory_portal() else 404
            assert client.post(launch, json={}, headers={"Origin": address}).status_code == expected_status
            assert client.post(launch, json={}, headers={
                "Origin": "http://localhost:8000", "Authorization": f"Bearer {key}",
            }).status_code == 404
            headers = {
                "Authorization": f"Bearer {key}",
                "X-Org-Id": "unexpected-org",
                "X-Principal-Type": "user",
                "X-Principal-Id": "unexpected-user",
            }
            response = client.post(f"{address}/v1/list", json=body, headers=headers)
            assert response.status_code == 200, response.text
            assert response.json()["items"] == []
    finally:
        assert manager.stop() == {"running": False}
    assert manager.stop() == {"running": False}
    with httpx.Client(trust_env=False, timeout=3) as client:
        try:
            client.get(f"{address}/healthz")
        except httpx.ConnectError:
            pass
        else:
            raise AssertionError("Managed HTTP listener did not close")


def test_managed_http_portal_can_save_and_reload_model(tmp_path, monkeypatch):
    if not managed.supports_managed_memory_portal():
        pytest.skip("Installed JiuwenMemory does not provide the managed HTTP Portal")
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    manager = managed.ManagedMemoryHttp()
    options = {
        "identity_org": "portal-org", "identity_user": "portal-owner",
        "sdk": {
            "embedder": {"default": "hashing"},
            "globals": {
                "embedder_dim": 64, "llm_model": "startup-model",
                "llm_base_url": "http://127.0.0.1:1/v1", "llm_api_key": "startup-test-secret",
            },
        },
    }
    field_key = "llm.default.params.llm_model"

    def portal_request(client, endpoint, payload):
        response = client.post(endpoint, json=payload)
        response.raise_for_status()
        return response.json()

    try:
        for expected_model in ("startup-model", "portal-model"):
            started = manager.start({}, options)
            address = started["base_url"]
            with httpx.Client(base_url=address, trust_env=False, timeout=10) as browser:
                browser.headers["Origin"] = address
                assert browser.post("/v1/save_config", json={"version": 0, "changes": {
                    field_key: "unauthorized-model",
                }}).status_code == 401
                link = browser.post("/portal/api/launch", json={}, headers={
                    "Authorization": f"Bearer {started['api_key']}",
                })
                link.raise_for_status()
                token = parse_qs(urlsplit(link.json()["portal_url"]).fragment)["access"][0]
                browser.headers["X-Memory-Portal-Browser"] = "1"
                session = portal_request(browser, "/portal/api/bootstrap", {"token": token, "embedded": True})
                browser.headers["X-Memory-Portal-Session"] = session["session_token"]

                config = portal_request(browser, "/v1/read_config", {})
                assert config["available"] is True
                assert "startup-test-secret" not in json.dumps(config)
                field = next(field for field in config["fields"] if field["key"] == field_key)
                assert field["writable"] is True
                assert field["effective"] == expected_model
                if expected_model == "startup-model":
                    saved = portal_request(browser, "/v1/save_config", {
                        "version": config["version"], "changes": {field_key: "portal-model"},
                    })
                    assert saved == {"version": config["version"] + 1, "applied": True}
                    current = portal_request(browser, "/v1/read_config", {})
                    current_field = next(field for field in current["fields"] if field["key"] == field_key)
                    assert current_field["effective"] == "portal-model"
            manager.stop()
        assert (tmp_path / "memory/managed-http/memory/.shadow/portal-config.db").is_file()
    finally:
        manager.stop()


def test_stop_defers_runtime_close_until_active_handler_finishes(tmp_path, monkeypatch):
    entered = Event()
    release = Event()
    runtime_closed = Event()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            entered.set()
            release.wait(timeout=10)

    server = SimpleNamespace(
        start=lambda: None,
        close=runtime_closed.set,
        handler_cls=lambda: Handler,
        authenticator=SimpleNamespace(mode=lambda: "test"),
        api=SimpleNamespace(),
    )
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(managed, "_build_server", lambda *_args: server)
    monkeypatch.setattr(managed, "_SHUTDOWN_TIMEOUT", 1.0)
    manager = managed.ManagedMemoryHttp()
    address = manager.start({}, {})["base_url"]

    with ThreadPoolExecutor(max_workers=1) as executor, httpx.Client(trust_env=False, timeout=5) as client:
        request = executor.submit(client.get, address)
        try:
            assert entered.wait(timeout=3)
            with pytest.raises(TimeoutError):
                manager.stop()
            assert manager.status() == {"running": False}
            assert not runtime_closed.is_set()
            with pytest.raises(ValueError, match="still shutting down"):
                manager.start({}, {})
            with pytest.raises(httpx.ConnectError):
                client.get(address)
        finally:
            release.set()
            assert runtime_closed.wait(timeout=3)
            assert manager.stop() == {"running": False}
        with pytest.raises(httpx.TransportError):
            request.result(timeout=3)


@pytest.mark.asyncio
@pytest.mark.parametrize("startup_fails", [False, True])
async def test_workswarm_starts_built_in_memory_http_automatically(tmp_path, monkeypatch, startup_fails):
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    host = {"plugins": {plugin.MEMORY_PLUGIN_ID: {
        "mode": "sdk", "base_url": "https://unused.example.test", "api_key": "old-key",
        "identity_user": "local-tester",
        "sdk": {"globals": {"embedder_dim": 64}, "embedder": {"default": "hashing"}},
    }}}
    monkeypatch.setattr(managed, "get_config", lambda: host)
    monkeypatch.setattr(agent_ws_server, "reset_harness_packages_state", lambda: None)
    monkeypatch.setattr("jiuwenswarm.common.auth.remote_config.warm_up_in_background", lambda: None)
    if startup_fails:
        host["plugins"][plugin.MEMORY_PLUGIN_ID] = None
    server = agent_ws_server.AgentWebSocketServer()
    runtime_started = asyncio.Event()

    async def start_runtime():
        runtime_started.set()

    monkeypatch.setattr(server._runtime, "start", start_runtime)
    monkeypatch.setattr(server._heartbeat_runtime, "start", AsyncMock())
    monkeypatch.setattr(server._jiuwenmate_web, "start", AsyncMock())
    monkeypatch.setattr(server, "_bootstrap_internal_jiuwenbox", AsyncMock())

    try:
        await server.start(bind_transport=False)
        await asyncio.wait_for(runtime_started.wait(), timeout=10)
        if startup_fails:
            assert managed.public_memory_plugin_settings() == {"mode": "http", "managed_http": {"running": False}}
            return
        options = plugin.resolve_memory_plugin_options(host, {"mode": "sdk"}, host["plugins"][plugin.MEMORY_PLUGIN_ID])
        assert options["mode"] == "http"
        assert options["base_url"].startswith("http://127.0.0.1:")
        assert options["api_key"] != "old-key"
        assert options["identity_user"] == "local-tester"
        assert managed.public_memory_plugin_settings() == {
            "mode": "http", "managed_http": {"running": True, "base_url": options["base_url"]},
        }
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.post(
                f"{options['base_url']}/v1/list",
                headers={"Authorization": f"Bearer {options['api_key']}"},
                json={"scope": {"org": "local", "user": "local-tester"}, "limit": 1},
            )
        assert response.status_code == 200, response.text
    finally:
        await server.stop()

    assert managed.managed_memory_http.status() == {"running": False}
    with pytest.raises(ValueError, match="service is not running"):
        plugin.resolve_memory_plugin_options(host, {}, {})


def test_enterprise_does_not_start_a_shared_personal_memory_service(monkeypatch):
    start = AsyncMock()
    monkeypatch.setattr(managed, "is_enterprise", lambda: True)
    monkeypatch.setattr(plugin, "is_enterprise", lambda: True)
    monkeypatch.setattr(managed.managed_memory_http, "start", start)

    managed.start_bundled_memory_http()

    start.assert_not_called()
    with pytest.raises(ValueError, match="multi-tenant"):
        plugin.resolve_memory_plugin_options({}, {}, {})


def test_desktop_gateway_does_not_register_memory_configuration_methods(monkeypatch):
    from agent_gateway.common.request_method import parse_request_method

    from jiuwenswarm.channels.desktop.gateway_entry import main

    monkeypatch.setattr("agent_gateway.dotenv_early.parse_dotenv_early", lambda: None)
    monkeypatch.setattr("jiuwenswarm.common.utils.prepare_local_gateway_config", lambda _env: None)
    monkeypatch.setitem(sys.modules, "agent_gateway.app_gateway", SimpleNamespace(main=lambda: None))
    main()

    assert parse_request_method("memory_plugin.config.save") is None
    assert parse_request_method("memory_plugin.connection.test") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("mode", ["sdk", "http"])
async def test_team_offline_does_not_construct_a_legacy_client(monkeypatch, mode):
    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder

    def reject_legacy_options(*_args, **_kwargs):
        pytest.fail("Offline Team memory must not resolve legacy connection options")

    monkeypatch.setattr(jiuwen_rail_builder, "memory_connection_options", reject_legacy_options)
    monkeypatch.setattr(managed, "managed_memory_http", managed.ManagedMemoryHttp())
    config = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {"mode": mode}}}}
    context = SwarmBuildContext(config=config, mode="team.code.normal",
                                memory_project="project-test", memory_team="team-test")
    assert not await prepare_team_memory(context)
    assert context.extras["team.memory_members_ready"] is False


@pytest.fixture
def require_collective_management():
    if not supports_team_memory_membership():
        pytest.skip("Installed JiuwenMemory does not support collective membership management")


@pytest.mark.asyncio
async def test_legacy_memory_preserves_revocations_and_serves_personal_http(tmp_path, monkeypatch):
    from jiuwenswarm.agents.harness.common.memory import team_memory_members

    if supports_team_memory_membership():
        monkeypatch.setattr(team_memory_members, "supports_team_memory_membership", lambda: False)
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    options = {"sdk": {"embedder": {"default": "hashing"}, "globals": {"embedder_dim": 64}}}
    config = {"memory": {"builtin": {"provider": "jiuwen"}}}
    monkeypatch.setattr(managed, "bundled_memory_http_config", lambda: (config, options))
    manager = managed.ManagedMemoryHttp()
    monkeypatch.setattr(managed, "managed_memory_http", manager)
    context = SwarmBuildContext(config=config, mode="team.code.normal",
                                memory_project="test-project", memory_team="test-team")
    await revoke_expert_group_memory("studio")

    started = manager.start(config, options)
    try:
        assert not await prepare_team_memory(context)
        assert context.extras["team.memory_members_ready"] is False
        change = context.extras["team.on_explicit_member_change"]
        await change("poster-instance", {"name": "poster"}, True)
        await change("poster-instance", {"name": "poster"}, False)
        async with httpx.AsyncClient(trust_env=False, timeout=5) as client:
            response = await client.post(
                f"{started['base_url']}/v1/list",
                json={"scope": {"org": "local", "user": "developer"}, "limit": 1},
                headers={"Authorization": f"Bearer {started['api_key']}"},
            )
        assert response.status_code == 200, response.text
    finally:
        manager.stop()
    with closing(sqlite3.connect(tmp_path / "memory" / "managed-http" / "pending-revocations.sqlite3")) as db:
        assert db.execute("SELECT kind, subject FROM pending ORDER BY id").fetchall() == [
            ("group", "studio"), ("member", "expert:poster"),
        ]


@pytest.mark.asyncio
@pytest.mark.usefixtures("require_collective_management")
async def test_offline_revocations_are_replayed_before_listener_is_available(tmp_path, monkeypatch):
    from jiuwen_memory.api import COLLECTIVE_MANAGER_ID, Dimension, Scope, Surface, new_request_context
    from jiuwen_memory.common.security.types import AuthContext, Role

    from jiuwenswarm.agents.harness.common.memory import jiuwen_rail_builder

    config = {"memory": {"builtin": {"provider": "jiuwen"}}}
    options = {
        "identity_org": "test-org", "identity_user": "test-user",
        "sdk": {"embedder": {"default": "hashing"}, "globals": {"embedder_dim": 64}},
    }
    monkeypatch.setenv("JIUWENSWARM_EDITION", "personal")
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(jiuwen_rail_builder, "load_memory_config", lambda: config)
    monkeypatch.setattr(managed, "bundled_memory_http_config", lambda: (config, options))
    manager = managed.ManagedMemoryHttp()
    monkeypatch.setattr(managed, "managed_memory_http", manager)
    group_team = team_memory_key("test-org", "", "test-user", "studio")
    group = SwarmBuildContext(config=config, mode="team.code.normal", memory_team=group_team,
                              memory_project="group-project", memory_group="studio", memory_experts=("leader",))
    team = SwarmBuildContext(config=config, mode="team.code.normal", memory_team="test-team",
                             memory_project="test-project")
    change = team.extras["team.on_explicit_member_change"]
    admin = new_request_context(
        AuthContext(actor=Scope(org="test-org", user=COLLECTIVE_MANAGER_ID), role=Role.ADMIN,
                    credential_type="internal", auth_method="internal", authenticated_at=datetime.now(UTC)),
        surface=Surface.INTERNAL,
    )

    def active(listener, kind, identifier):
        dimension = Dimension("test-org", "", kind, identifier)
        return {member.scope.agent for member in listener.call_collective_management(
            "list_collective_members", dimension, status="active", security=admin,
        )}

    manager.start(config, options)
    try:
        assert await prepare_team_memory(group)
        assert await prepare_team_memory(team)
        await change("poster-instance", {"name": "poster"}, True)
        await change("writer-instance", {"name": "writer"}, True)
        assert active(manager, "team", group_team) == {"main_agent", "studio:leader"}
        assert active(manager, "team", "test-team") == {"main_agent", "expert:poster", "expert:writer"}
    finally:
        manager.stop()

    await revoke_expert_group_memory("studio")
    await revoke_expert_template_memory("writer")
    await change("poster-instance", {"name": "poster"}, False)

    restarted = managed.ManagedMemoryHttp()
    monkeypatch.setattr(managed, "managed_memory_http", restarted)
    restarted.start(config, options)
    try:
        assert active(restarted, "team", group_team) == set()
        assert "studio:leader" in active(restarted, "project", "group-project")
        assert active(restarted, "team", "test-team") == {"main_agent"}
        assert "expert:poster" not in active(restarted, "project", "test-project")
        assert await prepare_team_memory(team)
        await change("poster-instance", {"name": "poster"}, True)
    finally:
        restarted.stop()

    restarted.start(config, options)
    try:
        assert "expert:poster" in active(restarted, "team", "test-team")
    finally:
        restarted.stop()


@pytest.mark.asyncio
@pytest.mark.usefixtures("require_collective_management")
async def test_team_rail_reads_managed_http_profile(tmp_path, monkeypatch):
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    manager = managed.ManagedMemoryHttp()
    monkeypatch.setattr(managed, "managed_memory_http", manager)
    manager.start({}, {"sdk": {"embedder": {"default": "hashing"}, "globals": {"embedder_dim": 64}}})
    try:
        legacy = {"memory": {"builtin": {"provider": "jiuwen", "jiuwen": {"mode": "sdk"}}}}
        context = SwarmBuildContext(config=legacy, mode="team.code.normal",
                                    memory_project="project-test", memory_team="team-test")
        assert await prepare_team_memory(context)
        assert context.extras["team.memory_members_ready"] is True
        assert not (tmp_path / "workspace" / "memory").exists()
        options = manager.connection_options()
        rail = build_jiuwen_memory_rail(
            {"memory": {"builtin": {"jiuwen": {"mode": "http", **options}}}},
            scene="team", project="project-test", team_binding={"team": "team-test"},
        )
        assert rail is not None
        try:
            await rail._provider.initialize(**rail._identity)
            profile = json.loads(await rail._provider.handle_tool_call(
                "team_memory_read", {"target": "profile", "name": "USER.md"},
            ))
            assert "error" not in profile, profile
            async with httpx.AsyncClient(trust_env=False) as client:
                response = await client.post(
                    f"{options['base_url']}/v1/list_collective_members",
                    json={"dimension": {"org": "local", "space": "", "kind": "team", "id": "team-test"}},
                    headers={"Authorization": f"Bearer {options['api_key']}"},
                )
            assert response.status_code == 403, response.text
        finally:
            await rail._provider.shutdown()
    finally:
        manager.stop()


def test_stop_defers_runtime_close_until_collective_operation_finishes(tmp_path, monkeypatch):
    entered = Event()
    release = Event()
    runtime_closed = Event()

    def list_members():
        entered.set()
        assert release.wait(timeout=10)
        return []

    server = SimpleNamespace(
        start=lambda: None,
        close=runtime_closed.set,
        handler_cls=lambda: BaseHTTPRequestHandler,
        authenticator=SimpleNamespace(mode=lambda: "test"),
        api=SimpleNamespace(list_collective_members=list_members),
    )
    monkeypatch.setattr(managed, "get_agent_root_dir", lambda: tmp_path)
    monkeypatch.setattr(managed, "_build_server", lambda *_args: server)
    monkeypatch.setattr(managed, "_SHUTDOWN_TIMEOUT", 1.0)
    manager = managed.ManagedMemoryHttp()
    manager.start({}, {})

    with ThreadPoolExecutor(max_workers=2) as executor:
        operation = executor.submit(manager.call_collective_management, "list_collective_members")
        try:
            assert entered.wait(timeout=3)
            stop = executor.submit(manager.stop)
            done, _ = wait([stop], timeout=3)
            assert stop in done
            with pytest.raises(TimeoutError):
                stop.result()
            assert manager.status() == {"running": False}
            assert not runtime_closed.is_set()
            with pytest.raises(ValueError, match="not running"):
                manager.call_collective_management("list_collective_members")
        finally:
            release.set()
            assert operation.result(timeout=3) == []
            assert runtime_closed.wait(timeout=3)
            assert manager.stop() == {"running": False}


@pytest.mark.skipif(sys.platform != "win32", reason="Windows directory junctions")
def test_profile_migration_preserves_existing_junctions(tmp_path: Path, monkeypatch) -> None:
    import _winapi

    profile = tmp_path / "profile"
    checkpoint = profile / "agent" / ".checkpoint" / "checkpoint.db"
    checkpoint.parent.mkdir(parents=True)
    checkpoint.write_bytes(b"checkpoint")
    core_logs = tmp_path / "logs" / "core"
    core_logs.mkdir(parents=True)
    runtime_log = core_logs / "runtime.log"
    runtime_log.write_bytes(b"runtime log")
    aliases = (
        (tmp_path / "agent", checkpoint.parent.parent),
        (tmp_path / "workspace", profile),
        (tmp_path / "logs" / "logs", core_logs),
    )
    for alias, target in aliases:
        _winapi.CreateJunction(str(target), str(alias))
    monkeypatch.setattr(paths, "_workspace_base_dir", tmp_path)
    monkeypatch.setattr(paths, "_legacy_migration_done", False)

    paths._do_migrate_profile_data_root_layout()

    assert checkpoint.read_bytes() == b"checkpoint"
    assert runtime_log.read_bytes() == b"runtime log"
    for alias, target in aliases:
        assert alias.samefile(target)


def test_migration_does_not_unlink_same_file(tmp_path: Path) -> None:
    checkpoint = tmp_path / "checkpoint.db"
    checkpoint.write_bytes(b"checkpoint")

    paths._migrate_absorb_file(checkpoint, checkpoint)

    assert checkpoint.read_bytes() == b"checkpoint"


def test_migration_merges_distinct_legacy_data(tmp_path: Path) -> None:
    old = tmp_path / "agent"
    new = tmp_path / "profile" / "agent"
    old.mkdir()
    new.mkdir(parents=True)
    (old / "duplicate.db").write_bytes(b"duplicate")
    (new / "duplicate.db").write_bytes(b"duplicate")
    (old / "checkpoint.db").write_bytes(b"old")
    (new / "checkpoint.db").write_bytes(b"new")
    (old / "legacy.db").write_bytes(b"legacy")

    paths._move_if_absent(old, new)

    assert not old.exists()
    assert (new / "duplicate.db").read_bytes() == b"duplicate"
    assert (new / "checkpoint.db").read_bytes() == b"new"
    assert (new / "checkpoint.db.migrated").read_bytes() == b"old"
    assert (new / "legacy.db").read_bytes() == b"legacy"
