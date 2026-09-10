"""Tests for Xiaoyi memory state and command responses."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.agents.harness.common.memory.celia.runtime_state import (
    ensure_runtime_state,
    read_memory_state,
    read_runtime_values,
    set_memory_state,
    update_runtime_info,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query import (
    extract_memory_query,
    handle_memory_query,
    memory_query_command,
)
from jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.xiaoyi_connect import XiaoyiChannel
from jiuwenswarm.common.utils import prepare_workspace


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


def test_memory_state_query_wire_shape(tmp_path):
    runtime = tmp_path / ".xiaoyiruntime"
    base = {"jsonrpc": "2.0", "id": "m", "params": {"sessionId": "c", "id": "t"}}

    def context(action, params=None):
        message = dict(base)
        message["command"] = {
            "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
            "payload": {"action": action, "params": params or {}},
        }
        return extract_memory_query(message)

    assert handle_memory_query(context("MemoryStateSet", {"memoryState": True}), runtime_state_path=str(runtime)) == {"code": 0}
    answer = handle_memory_query(context("MemoryStateGet"), runtime_state_path=str(runtime))
    assert memory_query_command("MemoryStateGet", answer) == {
        "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
        "payload": {"action": "MemoryStateGet", "ans": {"memoryState": True}},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("wrapped", [False, True])
@pytest.mark.parametrize("action", ["MemoryStateGet", "MemoryStateSet", "UserMdQuery", "MemoryMdQuery", "MemoryHistory"])
async def test_memory_query_artifact_response_is_final(tmp_path, monkeypatch, wrapped, action):
    runtime = tmp_path / ".xiaoyiruntime"
    set_memory_state(True, str(runtime))
    monkeypatch.setattr(
        "jiuwenswarm.gateway.channel_manager.im_platforms.xiaoyi.memory_query.configured_runtime_state_path",
        lambda: str(runtime),
    )
    sent = []

    async def safe_send(url_key, wrapper):
        sent.append((url_key, wrapper))

    channel = XiaoyiChannel.__new__(XiaoyiChannel)
    channel.config = SimpleNamespace(agent_id="agent")
    channel._ws_connections = {"ws": object()}
    channel._safe_ws_send = safe_send
    message = {
        "jsonrpc": "2.0", "id": "message",
        "params": {"sessionId": "session", "id": "task", "command": {
            "header": {"namespace": "AgentEvent", "name": "MemoryQuery"},
            "payload": {"action": action, "params": {"memoryState": False}},
        }},
    }
    if wrapped:
        message = {"sessionId": "session", "taskId": "task", "msgDetail": json.dumps(message)}
    await channel._handle_raw_message(json.dumps(message))
    answer = {"error": f"Unknown action: {action}"}
    if action == "MemoryStateGet":
        answer = {"memoryState": True}
    elif action == "MemoryStateSet":
        answer = {"code": 0}
    command = memory_query_command(action, answer)
    assert read_memory_state(str(runtime)) is (action != "MemoryStateSet")
    assert len(sent) == 1
    payload = json.loads(sent[0][1]["msgDetail"])
    assert payload["id"] == "message"
    assert payload["result"]["kind"] == "artifact-update"
    assert payload["result"]["final"] is True
    assert payload["result"]["artifact"]["parts"][0]["data"]["commands"] == [command]


def test_workspace_init_preserves_memory_state_without_creating_legacy_backend(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = home / ".jiuwenswarm"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    prepare_workspace(overwrite=False, workspace_dir=data)
    assert not (data / "celia" / "bin").exists()
    assert not (data / "agent" / "workspace" / "USER.md").exists()
    assert not (data / "agent" / "workspace" / "MEMORY.md").exists()
    assert not (data / "agent" / "workspace" / "memory" / "celia_memory").exists()
    assert (home / ".openclaw" / ".xiaoyiruntime").read_text(encoding="utf-8") == "MEMORYSTATE=false\n"
    assert not (home / ".openclaw" / ".memory.log").exists()
    assert not (home / ".openclaw" / "logs" / "Celia_memory.log").exists()

    db = data / "agent" / "workspace" / "memory" / "celia_memory" / "celia_memory.db"
    # A previous installation's data must survive forced reinitialization.
    db.parent.mkdir(parents=True)
    db.write_bytes(b"existing-db")
    state = home / ".openclaw" / ".xiaoyiruntime"
    state.write_text("MEMORYSTATE=true\nCUSTOM=value\n", encoding="utf-8")
    prepare_workspace(overwrite=True, workspace_dir=data)
    assert not (data / "agent" / "workspace" / "USER.md").exists()
    assert not (data / "agent" / "workspace" / "MEMORY.md").exists()
    assert db.read_bytes() == b"existing-db"
    assert state.read_text(encoding="utf-8") == "MEMORYSTATE=true\nCUSTOM=value\n"
    assert not (home / ".openclaw" / ".memory.log").exists()
