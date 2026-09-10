"""Tests for Xiaoyi memory state, workspace files and UI responses."""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
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


def test_workspace_init_preserves_memory_state_without_creating_legacy_backend(tmp_path, monkeypatch):
    home = tmp_path / "home"
    data = home / ".jiuwenswarm"
    home.mkdir()
    monkeypatch.setattr(Path, "home", lambda: home)
    prepare_workspace(overwrite=False, workspace_dir=data)
    assert not (data / "celia" / "bin").exists()
    assert (data / "agent" / "workspace" / "USER.md").is_file()
    assert (data / "agent" / "workspace" / "MEMORY.md").is_file()
    assert not (data / "agent" / "workspace" / "memory" / "celia_memory").exists()
    assert (home / ".openclaw" / ".xiaoyiruntime").read_text(encoding="utf-8") == "MEMORYSTATE=false\n"
    assert (home / ".openclaw" / ".memory.log").is_file()
    assert not (home / ".openclaw" / "logs" / "Celia_memory.log").exists()

    user_md = data / "agent" / "workspace" / "USER.md"
    memory_md = data / "agent" / "workspace" / "MEMORY.md"
    db = data / "agent" / "workspace" / "memory" / "celia_memory" / "celia_memory.db"
    user_md.write_text("user marker data", encoding="utf-8")
    memory_md.write_text("memory marker data", encoding="utf-8")
    # A previous installation's data must survive forced reinitialization.
    db.parent.mkdir(parents=True)
    db.write_bytes(b"existing-db")
    state = home / ".openclaw" / ".xiaoyiruntime"
    history = home / ".openclaw" / ".memory.log"
    state.write_text("MEMORYSTATE=true\nCUSTOM=value\n", encoding="utf-8")
    history.write_text("existing history", encoding="utf-8")
    prepare_workspace(overwrite=True, workspace_dir=data)
    assert user_md.read_text(encoding="utf-8") == "user marker data"
    assert memory_md.read_text(encoding="utf-8") == "memory marker data"
    assert db.read_bytes() == b"existing-db"
    assert state.read_text(encoding="utf-8") == "MEMORYSTATE=true\nCUSTOM=value\n"
    assert history.read_text(encoding="utf-8") == "existing history"
