# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests ensuring skill index refreshes never block the event loop.

Covers GitHub #12645 / GitCode #6979:
- Hidden directory pruning and atomic writes in `_generate_agent_data_for_workspace`
- Offloading workspace index refreshes off the asyncio event loop across
  `skills.import_upload`, `skills.import_local`, `skills.teamskillshub.install`,
  `skills.teamskillshub.init`, `skills.list`, `SkillPackInstaller.commit`, and
  `skills.create_from_knowledge` finalization.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import os
import threading
import time
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
    HubPackageDownloader,
)
from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager
from jiuwenswarm.server.runtime.skill.skillpack_install import SkillPackInstaller
from tests.unit_tests.agentserver.test_skillpack_backend import HubFixture

pytestmark = pytest.mark.usefixtures("allow_macos_pytest_temp_sources")

_HUB_ZIP_URL = "https://openjiuwen-market.obs.ap-southeast-1.myhuaweicloud.com/plugins/nonblock-demo.zip"


def _skill_md(name: str, description: str = "demo skill", version: str = "1.0.0") -> str:
    return (
        "---\n"
        f"name: {name}\n"
        f"description: {description}\n"
        f"version: {version}\n"
        "---\n"
        "# Demo\n"
    )


def _skill_zip_bytes(name: str, *, version: str = "1.0.0") -> bytes:
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(f"{name}/SKILL.md", _skill_md(name, version=version))
    return buf.getvalue()


async def _run_with_heartbeat(coro: Any) -> tuple[Any, int]:
    """Run an awaitable alongside a 10ms asyncio heartbeat ticker and return (result, ticks)."""
    ticks = 0
    stop = asyncio.Event()

    async def _ticker() -> None:
        nonlocal ticks
        while not stop.is_set():
            ticks += 1
            await asyncio.sleep(0.01)

    ticker_task = asyncio.create_task(_ticker())
    try:
        result = await coro
    finally:
        stop.set()
        await ticker_task
    return result, ticks


def _install_slow_index_probe(
    monkeypatch: pytest.MonkeyPatch,
    *,
    delay_seconds: float = 0.2,
) -> list[int]:
    """Wrap _generate_agent_data_for_workspace to sleep and record calling thread IDs."""
    original = SkillManager._generate_agent_data_for_workspace
    seen_threads: list[int] = []

    def _slow_generate(workspace_root: Path) -> None:
        seen_threads.append(threading.get_ident())
        time.sleep(delay_seconds)
        original(workspace_root)

    monkeypatch.setattr(
        SkillManager,
        "_generate_agent_data_for_workspace",
        staticmethod(_slow_generate),
    )
    return seen_threads


def test_generate_agent_data_prunes_hidden_dirs_and_preserves_schema(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Hidden directories must be pruned in-place without descending into them."""
    workspace = tmp_path / "workspace"
    skills_dir = workspace / "skills" / "demo-skill"
    skills_dir.mkdir(parents=True)
    (skills_dir / "SKILL.md").write_text(_skill_md("demo-skill"), encoding="utf-8")
    (skills_dir / "guide_zh.md").write_text("# zh", encoding="utf-8")
    (skills_dir / "guide_en.md").write_text("# en", encoding="utf-8")
    (skills_dir / "notes.txt").write_text("plain", encoding="utf-8")
    (skills_dir / ".hidden-file.md").write_text("secret", encoding="utf-8")

    # Create hidden directories that would be expensive to traverse recursively.
    for hidden_rel in (
        ".archive/versions/content/v1",
        ".agent_history/sessions/s1",
        ".git/objects/aa",
        "skills/.skillpack/transactions/tx1",
        "skills/demo-skill/.archive/versions/content/ver-1",
    ):
        hidden_dir = workspace / hidden_rel
        hidden_dir.mkdir(parents=True)
        (hidden_dir / "ignored.md").write_text("must not be indexed", encoding="utf-8")

    # Symlinked directory should not be followed; broken symlink should be skipped.
    external_dir = tmp_path / "external_dir"
    external_dir.mkdir()
    (external_dir / "outside.md").write_text("outside", encoding="utf-8")
    try:
        os.symlink(external_dir, workspace / "linked_dir")
        os.symlink(tmp_path / "does_not_exist.md", skills_dir / "broken.md")
    except OSError:
        pass

    visited_dirs: list[str] = []
    real_walk = os.walk

    def _spy_walk(top: Any, *args: Any, **kwargs: Any):
        for dirpath, dirnames, filenames in real_walk(top, *args, **kwargs):
            visited_dirs.append(str(dirpath))
            yield dirpath, dirnames, filenames

    monkeypatch.setattr(os, "walk", _spy_walk)

    SkillManager._generate_agent_data_for_workspace(workspace)

    # Confirm os.walk never descended into any hidden directory.
    assert not any(
        any(part.startswith(".") for part in Path(d).relative_to(workspace.resolve()).parts)
        for d in visited_dirs
    ), f"Hidden directory was traversed: {visited_dirs}"

    output_path = workspace / "agent-data.json"
    assert output_path.is_file()
    data = json.loads(output_path.read_text(encoding="utf-8"))

    skill_folder_key = "workspace/skills/demo-skill"
    assert skill_folder_key in data
    entries = data[skill_folder_key]
    assert [item["name"] for item in entries] == ["SKILL.md", "guide.md", "notes.txt"]
    assert entries[0] == {
        "name": "SKILL.md",
        "path": "agent/workspace/skills/demo-skill/SKILL.md",
        "isMarkdown": True,
    }
    assert entries[1]["isMarkdown"] is True
    assert entries[2]["isMarkdown"] is False

    # Hidden files, hidden directories, symlinked dirs, and temp files are excluded.
    all_paths = [item["path"] for files in data.values() for item in files]
    assert not any("ignored.md" in p or ".hidden" in p or "outside.md" in p for p in all_paths)
    assert not any(p.endswith(".tmp") for p in all_paths)
    assert list(workspace.glob(".agent-data.*.tmp")) == []


def test_generate_agent_data_atomic_concurrent_writes(tmp_path: Path) -> None:
    """Concurrent workspace index refreshes must always yield valid JSON with no temp leaks."""
    workspace = tmp_path / "workspace"
    skill_dir = workspace / "skills" / "alpha"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(_skill_md("alpha"), encoding="utf-8")

    # Seed initial file so concurrent readers can parse immediately.
    SkillManager._generate_agent_data_for_workspace(workspace)
    output_path = workspace / "agent-data.json"
    errors: list[Exception] = []

    def _writer(_: int) -> None:
        try:
            for _ in range(5):
                SkillManager._generate_agent_data_for_workspace(workspace)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def _reader(_: int) -> None:
        try:
            for _ in range(20):
                parsed = json.loads(output_path.read_text(encoding="utf-8"))
                assert isinstance(parsed, dict)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    with ThreadPoolExecutor(max_workers=10) as pool:
        futures = [pool.submit(_writer, i) for i in range(6)] + [
            pool.submit(_reader, i) for i in range(4)
        ]
        for fut in futures:
            fut.result()

    assert errors == []
    final_data = json.loads(output_path.read_text(encoding="utf-8"))
    all_paths = [item["path"] for files in final_data.values() for item in files]
    assert not any(".tmp" in p for p in all_paths)
    assert list(workspace.glob(".agent-data.*.tmp")) == []


@pytest.mark.asyncio
async def test_import_upload_does_not_block_event_loop_during_index_refresh(
    manager: SkillManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skills.import_upload must keep the worker event-loop heartbeat alive during slow index scans."""
    loop_thread = threading.get_ident()
    seen_threads = _install_slow_index_probe(monkeypatch, delay_seconds=0.2)

    zip_path = tmp_path / "upload-nonblock.zip"
    zip_path.write_bytes(_skill_zip_bytes("upload-nonblock"))

    result, ticks = await _run_with_heartbeat(
        manager.handle_skills_import_upload({"path": str(zip_path), "overwrite": False}),
    )

    assert result["success"] is True
    assert result["skill"]["name"] == "upload-nonblock"
    assert seen_threads and all(tid != loop_thread for tid in seen_threads)
    assert ticks >= 8
    index_data = json.loads((manager._agent_root / "agent-data.json").read_text(encoding="utf-8"))
    assert any(
        item["path"].endswith("skills/upload-nonblock/SKILL.md")
        for files in index_data.values()
        for item in files
    )


@pytest.mark.asyncio
async def test_import_local_does_not_block_event_loop_during_index_refresh(
    manager: SkillManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skills.import_local must run local import and index refresh off the event loop."""
    loop_thread = threading.get_ident()
    seen_threads = _install_slow_index_probe(monkeypatch, delay_seconds=0.2)

    src_dir = tmp_path / "local-nonblock"
    src_dir.mkdir()
    (src_dir / "SKILL.md").write_text(_skill_md("local-nonblock"), encoding="utf-8")

    result, ticks = await _run_with_heartbeat(
        manager.handle_skills_import_local({"path": str(src_dir), "force": False}),
    )

    assert result["success"] is True
    assert result["skill"]["name"] == "local-nonblock"
    assert seen_threads and all(tid != loop_thread for tid in seen_threads)
    assert ticks >= 8


@pytest.mark.asyncio
async def test_team_skills_hub_install_does_not_block_event_loop_during_index_refresh(
    manager: SkillManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skills.teamskillshub.install must keep the event-loop heartbeat alive during commit & index refresh."""
    loop_thread = threading.get_ident()
    seen_threads = _install_slow_index_probe(monkeypatch, delay_seconds=0.2)

    zip_bytes = _skill_zip_bytes("hub-nonblock", version="1.0.0")
    digest = hashlib.sha256(zip_bytes).hexdigest()

    async def _fake_get_data(path: str, **kwargs: Any) -> dict[str, Any]:
        _ = (path, kwargs)
        return {
            "asset_id": "hub-nonblock",
            "name": "hub-nonblock",
            "version": "1.0.0",
            "checksum_sha256": digest,
            "download_url": _HUB_ZIP_URL,
        }

    async def _fake_download(url: str, *, checksum_sha256: str = "") -> bytes:
        _ = (url, checksum_sha256)
        return zip_bytes

    monkeypatch.setattr(manager, "_team_skills_hub_http_get_data", _fake_get_data)
    monkeypatch.setattr(manager, "_download_zip_and_verify", _fake_download)

    result, ticks = await _run_with_heartbeat(
        manager.handle_skills_team_skills_hub_install({"asset_id": "hub-nonblock"}),
    )

    assert result["success"] is True
    assert result["skill"]["name"] == "hub-nonblock"
    assert seen_threads and all(tid != loop_thread for tid in seen_threads)
    assert ticks >= 8


@pytest.mark.asyncio
async def test_team_skills_hub_init_and_list_do_not_block_event_loop(
    manager: SkillManager,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """skills.teamskillshub.init and unmanaged discovery in skills.list must not block the event loop."""
    loop_thread = threading.get_ident()
    seen_threads = _install_slow_index_probe(monkeypatch, delay_seconds=0.2)

    init_result, init_ticks = await _run_with_heartbeat(
        manager.handle_skills_team_skills_hub_init({"name": "init-nonblock", "type": "skill"}),
    )
    assert init_result["success"] is True
    assert seen_threads and all(tid != loop_thread for tid in seen_threads)
    assert init_ticks >= 8

    # Drop an unmanaged local skill on disk and verify non-enterprise skills.list offloads discovery + refresh.
    seen_threads.clear()
    unmanaged = manager._skills_dir / "manual-nonblock"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text(_skill_md("manual-nonblock"), encoding="utf-8")

    list_result, list_ticks = await _run_with_heartbeat(
        manager.handle_skills_list({}),
    )
    assert any(s.get("name") == "manual-nonblock" for s in list_result["skills"])
    assert seen_threads and all(tid != loop_thread for tid in seen_threads)
    assert list_ticks >= 8


@pytest.mark.asyncio
async def test_skillpack_commit_does_not_block_event_loop_during_on_committed(
    tmp_path: Path,
) -> None:
    """SkillPackInstaller.commit must execute mutex-guarded commit & on_committed off the event loop."""
    workspace = tmp_path / "workspace"
    manager = SkillManager(workspace_dir=str(workspace))
    hub = HubFixture()
    installer = SkillPackInstaller(
        manager._skills_dir,
        "https://localhost",
        hub,
        state_files=(manager._state_file, manager._agent_root / "agent-data.json"),
        downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
        enabled_for=manager.get_skill_enabled,
    )
    artifact = hub.artifacts["release-pack"]
    plan = await installer.prepare(artifact, hub.bodies[artifact.download_url])

    loop_thread = threading.get_ident()
    callback_threads: list[int] = []

    def _slow_on_committed(records: list[dict[str, Any]]) -> None:
        callback_threads.append(threading.get_ident())
        time.sleep(0.2)
        manager._register_skillpack_install(records)

    committed, ticks = await _run_with_heartbeat(
        installer.commit(plan["plan_id"], on_committed=_slow_on_committed),
    )

    assert committed["success"] is True
    assert callback_threads == [callback_threads[0]]
    assert callback_threads[0] != loop_thread
    assert ticks >= 8


@pytest.mark.asyncio
async def test_create_from_knowledge_finalize_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """interface._handle_skills_create_from_knowledge must offload finalize_create_from_knowledge."""
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    out_dir = tmp_path / "knowledge-out"
    gen_dir = out_dir / "from-knowledge-nonblock"
    gen_dir.mkdir(parents=True)
    (gen_dir / "SKILL.md").write_text(_skill_md("from-knowledge-nonblock"), encoding="utf-8")

    loop_thread = threading.get_ident()
    seen_threads = _install_slow_index_probe(monkeypatch, delay_seconds=0.2)

    class _FakeAdapter:
        async def process_message_stream_impl(self, request: Any, inputs: Any):
            _ = (request, inputs)
            for _item in ():
                yield _item

    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = manager
    swarm._refresh_skill_rails_after_change = AsyncMock()
    swarm.create_instance = AsyncMock()
    swarm._reload_team_skill_rails = AsyncMock()
    monkeypatch.setattr(swarm, "_ensure_adapter", lambda **_kwargs: _FakeAdapter())
    monkeypatch.setattr(
        swarm,
        "_build_inputs",
        lambda request: ({"query": request.params.get("query")}, "disabled", ""),
    )

    async def _fake_prepare(params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        return {
            "success": True,
            "result_type": "followup",
            "followup_prompt": "generate skill",
            "skills": ["skill-omni-creation"],
            "output_dir": str(out_dir),
            "trusted_dirs": [str(out_dir)],
            "input_file": "",
        }

    monkeypatch.setattr(manager, "handle_skills_create_from_knowledge", _fake_prepare)

    req = AgentRequest(
        request_id="req-knowledge-nonblock",
        channel_id="web",
        session_id="sess-knowledge-nonblock",
        req_method=ReqMethod.SKILLS_CREATE_FROM_KNOWLEDGE,
        params={"link": "https://example.com/doc"},
    )

    resp, ticks = await _run_with_heartbeat(swarm._handle_skills_request(req))

    assert resp is not None
    assert resp.ok is True
    assert resp.payload["success"] is True
    assert resp.payload["skill"]["name"] == "from-knowledge-nonblock"
    assert seen_threads and all(tid != loop_thread for tid in seen_threads)
    assert ticks >= 8


@pytest.mark.asyncio
async def test_concurrent_import_uploads_serialize_cleanly_without_losing_uploads(
    manager: SkillManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Two overlapping skills.import_upload calls must queue cleanly and both succeed."""
    _install_slow_index_probe(monkeypatch, delay_seconds=0.15)
    zip_a = tmp_path / "batch-a.zip"
    zip_b = tmp_path / "batch-b.zip"
    zip_a.write_bytes(_skill_zip_bytes("batch-a"))
    zip_b.write_bytes(_skill_zip_bytes("batch-b"))

    results, ticks = await _run_with_heartbeat(
        asyncio.gather(
            manager.handle_skills_import_upload({"path": str(zip_a), "overwrite": False}),
            manager.handle_skills_import_upload({"path": str(zip_b), "overwrite": False}),
        ),
    )

    assert [r["success"] for r in results] == [True, True]
    assert {r["skill"]["name"] for r in results} == {"batch-a", "batch-b"}
    assert ticks >= 10
    assert (manager._skills_dir / "batch-a" / "SKILL.md").is_file()
    assert (manager._skills_dir / "batch-b" / "SKILL.md").is_file()
    index_data = json.loads((manager._agent_root / "agent-data.json").read_text(encoding="utf-8"))
    indexed_paths = {item["path"] for files in index_data.values() for item in files}
    assert any(p.endswith("skills/batch-a/SKILL.md") for p in indexed_paths)
    assert any(p.endswith("skills/batch-b/SKILL.md") for p in indexed_paths)


@pytest.mark.asyncio
async def test_import_upload_returns_inventory_busy_and_preserves_zip_on_lock_timeout(
    manager: SkillManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When inventory_mutex stays held past timeout, import_upload returns SKILL_INVENTORY_BUSY and keeps the zip."""
    from jiuwenswarm.server.runtime.skill import skill_manager as manager_module
    from jiuwenswarm.server.runtime.skill.skillpack_install import inventory_mutex

    monkeypatch.setattr(manager_module, "_SKILL_IMPORT_LOCK_TIMEOUT_SECONDS", 0.1)

    upload_dir = tmp_path / "jiuwenswarm_skill_upload_busy"
    upload_dir.mkdir()
    zip_path = upload_dir / "busy-skill.zip"
    zip_path.write_bytes(_skill_zip_bytes("busy-skill"))

    held = threading.Event()
    release = threading.Event()

    def _hold_mutex() -> None:
        with inventory_mutex(manager._skills_dir):
            held.set()
            release.wait(5)

    holder = threading.Thread(target=_hold_mutex)
    holder.start()
    try:
        assert await asyncio.to_thread(held.wait, 5)
        result = await manager.handle_skills_import_upload(
            {"path": str(zip_path), "overwrite": False},
        )
    finally:
        release.set()
        holder.join(timeout=5)

    assert result["success"] is False
    assert result["code"] == "SKILL_INVENTORY_BUSY"
    assert result["detail_key"] == "skills.inventoryBusy"
    assert "inventory.lock" not in result["detail"]
    assert zip_path.is_file(), "Uploaded zip must be preserved for retry on SKILL_INVENTORY_BUSY"


@pytest.mark.asyncio
async def test_concurrent_list_and_import_local_preserve_both_records_in_skills_state(
    manager: SkillManager,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Concurrent skills.list discovery and skills.import_local must preserve both skills_state.json records."""
    _install_slow_index_probe(monkeypatch, delay_seconds=0.1)

    unmanaged = manager._skills_dir / "manual-concurrent"
    unmanaged.mkdir(parents=True)
    (unmanaged / "SKILL.md").write_text(_skill_md("manual-concurrent"), encoding="utf-8")

    local_src = tmp_path / "imported-concurrent"
    local_src.mkdir()
    (local_src / "SKILL.md").write_text(_skill_md("imported-concurrent"), encoding="utf-8")

    list_result, import_result = await asyncio.gather(
        manager.handle_skills_list({}),
        manager.handle_skills_import_local({"path": str(local_src), "force": False}),
    )

    assert import_result["success"] is True
    assert any(s.get("name") == "manual-concurrent" for s in list_result["skills"])

    persisted = json.loads(manager._state_file.read_text(encoding="utf-8"))
    persisted_names = {item["name"] for item in persisted.get("local_skills", [])}
    assert {"manual-concurrent", "imported-concurrent"}.issubset(persisted_names)


@pytest.mark.asyncio
async def test_skillpack_commit_cancellation_waits_before_deleting_staging(
    tmp_path: Path,
) -> None:
    """Cancelling SkillPackInstaller.commit must not remove staging while the worker thread is still committing."""
    workspace = tmp_path / "workspace"
    manager = SkillManager(workspace_dir=str(workspace))
    hub = HubFixture()
    installer = SkillPackInstaller(
        manager._skills_dir,
        "https://localhost",
        hub,
        state_files=(manager._state_file, manager._agent_root / "agent-data.json"),
        downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
        enabled_for=manager.get_skill_enabled,
    )
    artifact = hub.artifacts["release-pack"]
    plan = await installer.prepare(artifact, hub.bodies[artifact.download_url])

    started = threading.Event()
    staging_alive_at_end: list[bool] = []

    def _slow_on_committed(records: list[dict[str, Any]]) -> None:
        started.set()
        time.sleep(0.15)
        staging_dirs = list(installer.work_dir.glob("skillpack-stage-*"))
        staging_alive_at_end.append(bool(staging_dirs and staging_dirs[0].is_dir()))
        manager._register_skillpack_install(records)

    task = asyncio.create_task(
        installer.commit(plan["plan_id"], on_committed=_slow_on_committed),
    )
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert staging_alive_at_end == [True]
    assert list(installer.work_dir.glob("skillpack-stage-*")) == []


@pytest.mark.asyncio
async def test_create_from_knowledge_cancellation_waits_before_deleting_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Cancelling create_from_knowledge must wait for finalize worker thread before finally deletes output_dir."""
    from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module

    manager = SkillManager(workspace_dir=str(tmp_path / "workspace"))
    out_dir = tmp_path / "knowledge-cancel-out"
    gen_dir = out_dir / "cancel-safe-skill"
    gen_dir.mkdir(parents=True)
    (gen_dir / "SKILL.md").write_text(_skill_md("cancel-safe-skill"), encoding="utf-8")

    started = threading.Event()
    output_alive_at_end: list[bool] = []
    original_finalize = manager.finalize_create_from_knowledge

    def _slow_finalize(*args: Any, **kwargs: Any) -> dict[str, Any]:
        started.set()
        time.sleep(0.15)
        output_alive_at_end.append(out_dir.is_dir())
        return original_finalize(*args, **kwargs)

    monkeypatch.setattr(manager, "finalize_create_from_knowledge", _slow_finalize)

    class _FakeAdapter:
        async def process_message_stream_impl(self, request: Any, inputs: Any):
            _ = (request, inputs)
            for _item in ():
                yield _item

    swarm = interface_module.JiuWenSwarm()
    swarm._skill_manager = manager
    swarm._refresh_skill_rails_after_change = AsyncMock()
    swarm.create_instance = AsyncMock()
    swarm._reload_team_skill_rails = AsyncMock()
    monkeypatch.setattr(swarm, "_ensure_adapter", lambda **_kwargs: _FakeAdapter())
    monkeypatch.setattr(
        swarm,
        "_build_inputs",
        lambda request: ({"query": request.params.get("query")}, "disabled", ""),
    )

    async def _fake_prepare(params: dict[str, Any]) -> dict[str, Any]:
        _ = params
        return {
            "success": True,
            "result_type": "followup",
            "followup_prompt": "generate skill",
            "skills": ["skill-omni-creation"],
            "output_dir": str(out_dir),
            "trusted_dirs": [str(out_dir)],
            "input_file": "",
        }

    monkeypatch.setattr(manager, "handle_skills_create_from_knowledge", _fake_prepare)

    req = AgentRequest(
        request_id="req-knowledge-cancel",
        channel_id="web",
        session_id="sess-knowledge-cancel",
        req_method=ReqMethod.SKILLS_CREATE_FROM_KNOWLEDGE,
        params={"link": "https://example.com/doc"},
    )

    task = asyncio.create_task(swarm._handle_skills_request(req))
    assert await asyncio.to_thread(started.wait, 5)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert output_alive_at_end == [True]
    assert not out_dir.exists()
