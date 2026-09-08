"""OpenClaw wire/file compatibility tests for Jiuwen Celia Memory."""

from __future__ import annotations

import json
from pathlib import Path
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest
from dataclasses import replace
from datetime import datetime, timedelta, timezone

from jiuwenswarm.agents.harness.common.memory.celia.config import (
    CeliaConfig,
    _endpoint,
    build_celia_config,
)
from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import (
    ensure_runtime_state,
    read_memory_state,
    read_runtime_values,
    set_memory_state,
    update_runtime_info,
)
from jiuwenswarm.agents.harness.common.memory.celia.provider import CeliaMemoryProvider
from jiuwenswarm.agents.harness.common.memory.celia.fixed_context import get_fixed_context_cache
from jiuwenswarm.agents.harness.common.memory.celia.runtime_store import get_runtime_store
from jiuwenswarm.agents.harness.common.memory.celia.client_manager import CeliaClientManager
from jiuwenswarm.agents.harness.common.memory.celia.workspace_sync import (
    OVERVIEW_MARKER,
    SCENES_MARKER,
    read_memory_history,
    safe_write_marker,
    select_fixed_scenes,
    sync_workspace_files,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
    extract_memory_query,
    handle_memory_query,
    memory_query_command,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import XiaoyiChannel
from jiuwenswarm.common.utils import prepare_workspace


def _config(**values) -> CeliaConfig:
    return CeliaConfig(
        server_binary_path="/tmp/celia",
        db_path="/tmp/celia.db",
        log_path="/tmp/Celia_memory.log",
        tenant_id="tenant",
        user_id="user",
        scope_id="user",
        runtime_state_path="/tmp/.xiaoyiruntime",
        **values,
    )


def test_runtime_store_defaults_false_and_preserves_unknown_keys(tmp_path):
    path = tmp_path / ".xiaoyiruntime"
    ensure_runtime_state(str(path))
    assert path.read_text(encoding="utf-8") == "MEMORYSTATE=false\n"
    path.write_text("export FOO='bar'\nMEMORYSTATE=0\nTASK_ID=old\n", encoding="utf-8")
    set_memory_state(True, str(path))
    update_runtime_info("socket", "conversation", "task", str(path))
    assert read_memory_state(str(path)) is True
    assert read_runtime_values(str(path)) == {
        "FOO": "bar",
        "MEMORYSTATE": "true",
        "TASK_ID": "task",
        "SESSION_ID": "socket",
        "CONVERSATION_ID": "conversation",
    }


def test_runtime_store_uses_last_assignment_and_rejects_invalid(tmp_path):
    path = tmp_path / ".xiaoyiruntime"
    path.write_text("MEMORYSTATE=false\nexport MEMORYSTATE='1'\n", encoding="utf-8")
    assert read_memory_state(str(path)) is True
    path.write_text("MEMORYSTATE=yes\n", encoding="utf-8")
    assert read_memory_state(str(path)) is False


def test_runtime_store_serializes_concurrent_key_updates(tmp_path):
    path = tmp_path / ".xiaoyiruntime"
    ensure_runtime_state(str(path))
    with ThreadPoolExecutor(max_workers=8) as pool:
        futures = [
            pool.submit(update_runtime_info, f"s{i}", f"c{i}", f"t{i}", str(path))
            for i in range(20)
        ]
        futures += [pool.submit(set_memory_state, i % 2 == 0, str(path)) for i in range(20)]
        for future in futures:
            future.result()
    values = read_runtime_values(str(path))
    assert set(("MEMORYSTATE", "SESSION_ID", "CONVERSATION_ID", "TASK_ID")) <= values.keys()


def test_memory_query_extracts_direct_and_wrapped_a2a():
    command = {
        "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
        "payload": {"action": "MemoryStateSet", "params": {"memoryState": True}},
    }
    direct = {"jsonrpc": "2.0", "id": "msg", "params": {"sessionId": "conv", "id": "task", "command": command}}
    wrapped = {"sessionId": "socket", "taskId": "task", "msgDetail": json.dumps(direct)}
    assert extract_memory_query(direct).params == {"memoryState": True}
    context = extract_memory_query(wrapped)
    assert context is not None
    assert context.action == "MemoryStateSet"
    assert context.session_id == "socket"
    assert context.task_id == "task"
    assert context.message_id == "msg"


def test_memory_query_five_actions_and_wire_shape(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / "USER.md").write_text("user detail", encoding="utf-8")
    (workspace / "MEMORY.md").write_text("memory detail", encoding="utf-8")
    runtime = tmp_path / ".xiaoyiruntime"
    history = tmp_path / ".memory.log"
    base = {"jsonrpc": "2.0", "id": "m", "params": {"sessionId": "c", "id": "t"}}

    def context(action, params=None):
        message = dict(base)
        message["command"] = {
            "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
            "payload": {"action": action, "params": params or {}},
        }
        return extract_memory_query(message)

    assert handle_memory_query(context("MemoryStateSet", {"memoryState": True}), workspace_dir=workspace, runtime_state_path=str(runtime)) == {"code": 0}
    answer = handle_memory_query(context("MemoryStateGet"), workspace_dir=workspace, runtime_state_path=str(runtime))
    assert memory_query_command("MemoryStateGet", answer) == {
        "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
        "payload": {"action": "MemoryStateGet", "ans": {"memoryState": True}},
    }
    assert handle_memory_query(context("UserMdQuery"), workspace_dir=workspace)["fileDetail"] == "user detail"
    assert handle_memory_query(context("MemoryMdQuery"), workspace_dir=workspace)["fileDetail"] == "memory detail"
    assert handle_memory_query(context("MemoryHistory"), workspace_dir=workspace, history_path=history) == []


@pytest.mark.asyncio
async def test_memory_query_artifact_response_is_final():
    sent = []

    async def safe_send(url_key, wrapper):
        sent.append((url_key, wrapper))

    channel = SimpleNamespace(
        config=SimpleNamespace(agent_id="agent"),
        _ws_connections={"ws": object()},
        _safe_ws_send=safe_send,
    )
    command = memory_query_command("MemoryStateGet", {"memoryState": False})
    assert await XiaoyiChannel.send_xiaoyi_phone_tools_command(
        channel, "session", "task", "message", command, final=True
    )
    payload = json.loads(sent[0][1]["msgDetail"])
    assert payload["id"] == "message"
    assert payload["result"]["kind"] == "artifact-update"
    assert payload["result"]["final"] is True
    assert payload["result"]["artifact"]["parts"][0]["data"]["commands"] == [command]


def test_fixed_scenes_are_scene_only_preset_plus_dynamic_top_ten():
    entries = [
        {"id": "memtype", "type": "memtype", "factCount": 999},
        {"id": "preset", "type": "scene", "is_preset": True, "factCount": 0},
        *({"id": f"s{i}", "type": "scene", "factCount": i} for i in range(15)),
    ]
    selected = select_fixed_scenes({"entries": entries})
    assert selected[0]["id"] == "preset"
    assert [item["id"] for item in selected[1:]] == [f"s{i}" for i in range(14, 4, -1)]
    assert all(item["id"] != "memtype" for item in selected)


def test_marker_sync_preserves_user_content_is_idempotent_and_repairs(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_md = workspace / "USER.md"
    memory_md = workspace / "MEMORY.md"
    user_md.write_text("# User authored\n", encoding="utf-8")
    memory_md.write_text("# Keep me\n", encoding="utf-8")
    history = tmp_path / ".memory.log"
    l0 = {"global_summary": "## User Profile\n- likes tea"}
    l1 = {"entries": [{"id": "food", "path": "scene/food", "type": "scene", "summary": "likes noodles", "factCount": 3}]}
    sync_workspace_files(workspace, l0, l1, history)
    first = user_md.read_text(encoding="utf-8")
    assert "# User authored" in first and OVERVIEW_MARKER in first
    assert "# Keep me" in memory_md.read_text(encoding="utf-8") and SCENES_MARKER in memory_md.read_text(encoding="utf-8")
    history_first = history.read_text(encoding="utf-8")
    sync_workspace_files(workspace, l0, l1, history)
    assert user_md.read_text(encoding="utf-8") == first
    assert history.read_text(encoding="utf-8") == history_first

    user_md.write_text(first + f"\n<!-- {OVERVIEW_MARKER}_BEGIN h=bad -->\nbroken", encoding="utf-8")
    safe_write_marker(user_md, OVERVIEW_MARKER, "new", 4096, history)
    assert user_md.read_text(encoding="utf-8").count(f"{OVERVIEW_MARKER}_BEGIN") == 1
    assert list(workspace.glob("USER.md.celia-rescue.*"))


def test_failed_fixed_fetch_keeps_existing_marker(tmp_path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    user_md = workspace / "USER.md"
    memory_md = workspace / "MEMORY.md"
    safe_write_marker(user_md, OVERVIEW_MARKER, "old overview", 4096)
    safe_write_marker(memory_md, SCENES_MARKER, "old scenes", 6144)
    overview, scenes = sync_workspace_files(
        workspace, None, None, tmp_path / ".memory.log", sync_l0=False, sync_l1=False
    )
    assert overview == "old overview"
    assert scenes == "old scenes"


@pytest.mark.asyncio
async def test_provider_fixed_load_never_loads_l1_full_text(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=true\n", encoding="utf-8")
    config = replace(
        _config(), workspace_dir=str(tmp_path), runtime_state_path=str(runtime)
    )

    class Client:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args, **kwargs):
            self.calls.append(name)
            if name == "memory_global_load":
                return {"global_summary": "profile", "navigation": [{"sceneId": "s", "summary": "brief"}]}
            raise AssertionError(name)

        async def load_l1_batch(self, *args, **kwargs):
            raise AssertionError("fixed load must not call memory_load_l1")

    class Sessions:
        async def ensure_tool_session(self, user_id):
            return f"tools-{user_id}"

    provider = CeliaMemoryProvider(config, user_id="user")
    client = Client()
    provider._lease = SimpleNamespace(client=client, sessions=Sessions())
    provider._initialized = True
    get_fixed_context_cache().clear()
    context = await provider.prefetch("query")
    assert "profile" in context and "brief" in context
    assert "CELIA_MEMORY_GUIDE" not in context
    assert "memory_load_l1" not in client.calls
    assert client.calls == ["memory_global_load"]


@pytest.mark.asyncio
async def test_provider_does_not_call_removed_round_usage_endpoint(tmp_path):
    get_runtime_store().clear_all()
    runtime = tmp_path / ".xiaoyiruntime"
    runtime.write_text("MEMORYSTATE=true\n", encoding="utf-8")

    class Client:
        def __init__(self):
            self.calls = []

        async def call_tool(self, name, args, **kwargs):
            self.calls.append((name, args))
            return {"status": 0}

    class Sessions:
        async def ensure_tool_session(self, user_id):
            return f"tools-{user_id}"

    provider = CeliaMemoryProvider(
        replace(_config(), runtime_state_path=str(runtime)), user_id="user"
    )
    client = Client()
    provider._lease = SimpleNamespace(client=client, sessions=Sessions())
    provider._initialized = True
    provider._supported_mcp_tools = {"memory_report_round_usage"}
    await provider.report_round_usage(
        prompt_tokens=10, cache_read_tokens=2, completion_tokens=4,
        llm_turns=2, recall_tokens=3, fixed_load_tokens=5,
    )
    assert client.calls == []


@pytest.mark.asyncio
async def test_client_manager_keeps_process_until_server_shutdown(monkeypatch):
    instances = []

    class Client:
        def __init__(self, config):
            self.started = 0
            self.closed = 0
            self.callbacks = []
            instances.append(self)

        def add_restart_callback(self, callback):
            self.callbacks.append(callback)

        async def start(self):
            self.started += 1

        async def close(self):
            self.closed += 1

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.memory.celia.client_manager.CeliaMcpClient",
        Client,
    )
    manager = CeliaClientManager()
    lease = await manager.acquire(_config())
    await manager.release(lease)
    assert instances[0].closed == 0
    second = await manager.acquire(_config())
    assert second.client is instances[0]
    await manager.release(second)
    await manager.close_all()
    assert instances[0].closed == 1


def test_memory_history_returns_seven_days_and_prunes_thirty(tmp_path):
    history = tmp_path / ".memory.log"
    now = datetime(2026, 7, 15, 12, tzinfo=timezone.utc)
    recent = now - timedelta(days=6)
    old = now - timedelta(days=31)
    history.write_text(
        f"{recent.isoformat()}|user.md|recent\n{old.isoformat()}|memory.md|old\n",
        encoding="utf-8",
    )
    assert [item["detail"] for item in read_memory_history(history, now)] == ["recent"]
    assert "old" not in history.read_text(encoding="utf-8")


def test_dream_inherit_does_not_set_child_environment():
    env = _config(dreaming_enabled="inherit").child_env({"CELIA_DREAMING_ENABLED": "true"})
    assert "CELIA_DREAMING_ENABLED" not in env
    assert replace(_config(), dreaming_enabled="on").child_env({})["CELIA_DREAMING_ENABLED"] == "true"


def test_endpoint_priority_is_source_local(monkeypatch):
    monkeypatch.setenv("OPENAI_CHAT_BASE_URL", "https://env.example")
    monkeypatch.setenv("OPENAI_CHAT_API_KEY", "env-key")
    endpoint = _endpoint(
        {"base_url": "https://incomplete.example", "model": "must-not-leak"},
        fallback={"base_url": "https://jiuwen.example", "api_key": "jiuwen-key", "model": "jiuwen-model"},
        env_prefix="CHAT",
        uid_env="CELIA_CHAT_UID",
    )
    assert endpoint.base_url == "https://jiuwen.example"
    assert endpoint.api_key == "jiuwen-key"
    assert endpoint.model == "jiuwen-model"


def test_empty_home_workspace_init_creates_celia_compatibility_state_without_binary(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = home / ".jiuwenswarm"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    prepare_workspace(overwrite=False, workspace_dir=data)
    binary_dir = data / "celia" / "bin"
    assert binary_dir.is_dir()
    assert not (binary_dir / "gspd_memory_mcp_server").exists()
    assert (data / "agent" / "workspace" / "USER.md").is_file()
    assert (data / "agent" / "workspace" / "MEMORY.md").is_file()
    assert (data / "agent" / "workspace" / "memory" / "celia_memory" / "celia_memory.db").is_file()
    assert (home / ".openclaw" / ".xiaoyiruntime").read_text(encoding="utf-8") == "MEMORYSTATE=false\n"
    assert (home / ".openclaw" / ".memory.log").is_file()
    assert (home / ".openclaw" / "logs" / "Celia_memory.log").is_file()

    user_md = data / "agent" / "workspace" / "USER.md"
    memory_md = data / "agent" / "workspace" / "MEMORY.md"
    db = data / "agent" / "workspace" / "memory" / "celia_memory" / "celia_memory.db"
    user_md.write_text("user marker data", encoding="utf-8")
    memory_md.write_text("memory marker data", encoding="utf-8")
    db.write_bytes(b"existing-db")
    prepare_workspace(overwrite=True, workspace_dir=data)
    assert user_md.read_text(encoding="utf-8") == "user marker data"
    assert memory_md.read_text(encoding="utf-8") == "memory marker data"
    assert db.read_bytes() == b"existing-db"


def test_celia_binary_uses_fixed_extension_path_then_legacy_fallback(tmp_path, monkeypatch):
    data_dir = tmp_path / ".jiuwenswarm"
    workspace = data_dir / "agent" / "workspace"
    monkeypatch.delenv("CELIA_MEMORY_BINARY_PATH", raising=False)
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir",
        lambda: data_dir,
    )

    package_dir = data_dir / "extensions" / "celia_memory" / "package"
    gspd_binary = package_dir / "openclaw" / "bin" / "gspd_memory_mcp_server"
    ignored_binary = package_dir / "bin" / "gspd_memory_mcp_server"
    gspd_binary.parent.mkdir(parents=True)
    ignored_binary.parent.mkdir(parents=True)
    gspd_binary.touch()
    ignored_binary.touch()

    resolved = build_celia_config(
        {},
        {"celia": {"server_binary_path": "/configured/legacy-binary"}},
        workspace_dir=str(workspace),
    )
    assert Path(resolved.server_binary_path) == gspd_binary

    gspd_binary.unlink()
    resolved = build_celia_config(
        {},
        {"celia": {"server_binary_path": "/configured/legacy-binary"}},
        workspace_dir=str(workspace),
    )
    assert Path(resolved.server_binary_path) == (
        data_dir / "celia" / "bin" / "gspd_memory_mcp_server"
    )
