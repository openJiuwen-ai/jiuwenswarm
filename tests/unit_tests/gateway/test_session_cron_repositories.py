# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""SessionMapRepository / CronJobRepository PersistentStore adapters."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.common.utils import resolve_gateway_cron_jobs_path_template
from jiuwenswarm.gateway.cron.job_access import (
    clear_cron_persistent_store,
    create_tenant_cron_store,
    set_cron_persistent_store,
)
from jiuwenswarm.gateway.cron.job_repository import CronJobRepository
from jiuwenswarm.gateway.routing.session_map import Session, SessionMap
from jiuwenswarm.gateway.routing.session_map_access import (
    PersistentSessionStorage,
    clear_session_map_repository,
    set_session_map_repository,
)
from jiuwenswarm.gateway.routing.session_map_repository import SessionMapRepository
from jiuwenswarm.gateway.storage.backends.file_persistent import FilePersistentBackend
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend
from jiuwenswarm.gateway.storage_assembly.layouts import build_gateway_store_registry


@pytest.mark.asyncio
async def test_session_map_repository_upsert_get() -> None:
    store = InMemoryPersistentBackend()
    repo = SessionMapRepository(store)
    sess = Session(session_id="s1", service_id="svc", agent_id="a1")
    await repo.upsert("feishu::chat::bot", sess)
    got = await repo.get("feishu::chat::bot")
    assert got is not None
    assert got.session_id == "s1"
    mapping = await repo.list_all()
    assert "feishu::chat::bot" in mapping


def test_persistent_session_storage_sync_bridge() -> None:
    store = InMemoryPersistentBackend()
    repo = SessionMapRepository(store)
    set_session_map_repository(repo)
    try:
        storage = PersistentSessionStorage(repo)
        sess = Session(session_id="s2", service_id="svc", agent_id=None)
        storage.set("web::c::b", sess)
        assert storage.get("web::c::b") is not None
        assert storage.get("web::c::b").session_id == "s2"
    finally:
        clear_session_map_repository()


def test_persistent_session_storage_personal_uses_local_cache() -> None:
    store = InMemoryPersistentBackend()
    repo = SessionMapRepository(store)
    storage = PersistentSessionStorage(repo, read_through=False)
    key = "feishu::chat::bot"
    storage.set(key, Session(session_id="sess-1", service_id="svc", agent_id=None))
    assert storage.get(key).session_id == "sess-1"
    # External write bypassing this instance's cache (same as before LocalSessionStorage).
    import asyncio

    asyncio.run(
        repo.upsert(key, Session(session_id="sess-2", service_id="svc", agent_id=None))
    )
    assert storage.get(key).session_id == "sess-1"


def test_persistent_session_storage_read_through_across_instances() -> None:
    """Pod B must see Pod A writes without stale local cache (P-04)."""
    store = InMemoryPersistentBackend()
    storage_a = PersistentSessionStorage(
        SessionMapRepository(store),
        read_through=True,
    )
    storage_b = PersistentSessionStorage(
        SessionMapRepository(store),
        read_through=True,
    )

    key = "feishu::chat::bot"
    storage_a.set(key, Session(session_id="sess-old", service_id="svc", agent_id=None))
    assert storage_b.get(key) is not None
    assert storage_b.get(key).session_id == "sess-old"

    storage_a.set(key, Session(session_id="sess-new", service_id="svc", agent_id=None))
    assert storage_b.get(key).session_id == "sess-new"

    all_rows = storage_b.get_all()
    assert all_rows[key].session_id == "sess-new"


def test_session_map_uses_persistent_storage_when_repository_wired() -> None:
    store = InMemoryPersistentBackend()
    repo = SessionMapRepository(store)
    set_session_map_repository(repo)
    try:
        session_map = SessionMap()
        assert isinstance(session_map._storage, PersistentSessionStorage)
    finally:
        clear_session_map_repository()


def test_personal_session_map_layout_uses_checkpoint_path(tmp_path) -> None:
    checkpoint = tmp_path / "service_default" / "agent_default" / ".checkpoint"
    checkpoint.mkdir(parents=True)
    workspace = tmp_path / "home"
    workspace.mkdir()
    config_file = workspace / "config.yaml"
    config_file.write_text("gateway:\n  edition: personal\n", encoding="utf-8")

    registry = build_gateway_store_registry(
        persistent_root=workspace / "gateway" / "persistent",
        config_file=config_file,
        session_map_file=checkpoint / "session_map.json",
    )
    layout = registry.get("session_map")
    assert layout.file is not None
    assert layout.file.path == str((checkpoint / "session_map.json").resolve())


def test_personal_cron_job_layout_uses_legacy_gateway_path(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_user_workspace_dir", lambda: tmp_path
    )
    workspace = tmp_path
    config_file = workspace / "config.yaml"
    config_file.write_text("gateway:\n  edition: personal\n", encoding="utf-8")

    registry = build_gateway_store_registry(
        persistent_root=workspace / "gateway" / "persistent",
        config_file=config_file,
        cron_jobs_path_template=resolve_gateway_cron_jobs_path_template(),
    )
    layout = registry.get("cron_job")
    assert layout is not None
    assert layout.file is not None
    assert layout.file.path == resolve_gateway_cron_jobs_path_template()
    assert layout.file.json_document_key == "jobs"


@pytest.mark.asyncio
async def test_file_persistent_reads_and_writes_legacy_cron_wrapper(tmp_path) -> None:
    cron_template = str(
        (
            tmp_path
            / "gateway"
            / "cron"
            / "service_{service_id}"
            / "agent_{agent_id}"
            / "cron_jobs.json"
        ).resolve()
    )
    cron_path = (
        tmp_path
        / "gateway"
        / "cron"
        / "service_default"
        / "agent_default"
        / "cron_jobs.json"
    )
    cron_path.parent.mkdir(parents=True)
    cron_path.write_text(
        json.dumps(
            {
                "version": 1,
                "jobs": [
                    {
                        "id": "job-legacy",
                        "name": "daily",
                        "enabled": True,
                        "service_id": "default",
                        "agent_id": "default",
                    }
                ],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    backend = FilePersistentBackend(
        registry=build_gateway_store_registry(
            persistent_root=tmp_path / "gateway" / "persistent",
            config_file=tmp_path / "config.yaml",
            cron_jobs_path_template=cron_template,
        )
    )
    rows = await backend.list(
        "cron_job",
        filters={"service_id": "default", "agent_id": "default"},
    )
    assert len(rows) == 1
    assert rows[0]["id"] == "job-legacy"

    await backend.update(
        "cron_job",
        {"id": "job-legacy", "service_id": "default", "agent_id": "default"},
        {"description": "updated via repository"},
    )
    saved = json.loads(cron_path.read_text(encoding="utf-8"))
    assert saved["version"] == 1
    assert isinstance(saved["jobs"], list)
    assert saved["jobs"][0]["description"] == "updated via repository"


@pytest.mark.asyncio
async def test_cron_job_repository_crud() -> None:
    store = InMemoryPersistentBackend()
    repo = CronJobRepository(store, service_id="default", agent_id="default")
    job = await repo.create_job(
        name="daily",
        cron_expr="0 9 * * *",
        timezone="Asia/Shanghai",
        description="d",
        targets="web",
    )
    assert job.id
    listed = await repo.list_jobs()
    assert len(listed) == 1
    updated = await repo.update_job(job.id, {"description": "updated"})
    assert updated.description == "updated"
    assert await repo.delete_job(job.id) is True
    assert await repo.list_jobs() == []


@pytest.mark.asyncio
async def test_create_tenant_cron_store_uses_repository_when_wired() -> None:
    store = InMemoryPersistentBackend()
    set_cron_persistent_store(store)
    try:
        backend = create_tenant_cron_store("svc1", "agent1")
        assert isinstance(backend, CronJobRepository)
        job = await backend.create_job(
            name="n",
            cron_expr="0 * * * *",
            timezone="UTC",
            description="d",
            targets="web",
        )
        assert job.service_id == "svc1"
        assert job.agent_id == "agent1"
        rows = await store.list(
            "cron_job",
            filters={"service_id": "svc1", "agent_id": "agent1"},
        )
        assert len(rows) == 1
    finally:
        clear_cron_persistent_store()
