# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业专属表 EnterpriseRecordRepository（不注入运行时）。"""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.config.enterprise import (
    ENTERPRISE_RECORD_STORE_NAMES,
    EnterpriseRecordRepository,
    clear_enterprise_record_repositories,
    get_enterprise_record_repository,
    set_enterprise_record_repositories,
)
from jiuwenswarm.gateway.storage.backends.memory_persistent import InMemoryPersistentBackend
from jiuwenswarm.gateway.storage_assembly.layouts import build_gateway_store_registry
from jiuwenswarm.gateway.storage_assembly.setup import (
    create_enterprise_record_repository,
    create_enterprise_record_repositories,
)


@pytest.mark.asyncio
async def test_instance_agent_resource_crud() -> None:
    store = InMemoryPersistentBackend()
    repo = EnterpriseRecordRepository(
        store,
        "instance_agent_resource",
        instance_id="",
    )
    created = await repo.create(
        {
            "resource_id": "bot-main",
            "resource_name": "main agent",
            "ref_template_id": "tmpl-1",
            "grants": [{"match_expr": "", "enabled": True}],
        }
    )
    assert "jiuwenclaw_id" not in created
    assert created["resource_id"] == "bot-main"

    got = await repo.get(resource_id="bot-main")
    assert got is not None
    assert got["resource_name"] == "main agent"

    updated = await repo.update(
        {"resource_id": "bot-main"},
        {"resource_name": "main agent v2"},
    )
    assert updated is not None
    assert updated["resource_name"] == "main agent v2"

    assert await repo.delete(resource_id="bot-main") is True
    assert await repo.get(resource_id="bot-main") is None


@pytest.mark.asyncio
async def test_template_upsert_and_sync() -> None:
    store = InMemoryPersistentBackend()
    repo = create_enterprise_record_repository(
        store, "model_template", instance_id="gw-1"
    )
    await repo.upsert(
        {
            "template_id": "m1",
            "template_name": "one",
            "api_base": "http://a",
            "api_key": "k",
            "model_id": "id",
            "model_provider": "p",
            "model_type": ["default"],
        }
    )
    await repo.upsert(
        {
            "template_id": "m2",
            "template_name": "two",
            "api_base": "http://b",
            "api_key": "k",
            "model_id": "id2",
            "model_provider": "p",
            "model_type": ["default"],
        }
    )
    result = await repo.sync_by_business_key(
        [
            {
                "template_id": "m1",
                "template_name": "one-renamed",
                "api_base": "http://a",
                "api_key": "k",
                "model_id": "id",
                "model_provider": "p",
                "model_type": ["default"],
            }
        ]
    )
    assert result == {"synced_count": 1, "deleted_count": 1}
    rows = await repo.list()
    assert len(rows) == 1
    assert rows[0]["template_id"] == "m1"
    assert rows[0]["template_name"] == "one-renamed"


@pytest.mark.asyncio
async def test_singleton_keypair_no_scope() -> None:
    store = InMemoryPersistentBackend()
    repo = EnterpriseRecordRepository(store, "gateway_enc_keypair")
    await repo.create(
        {
            "id": "default",
            "enc_alg": "x25519",
            "private_key": "priv",
            "public_key": "pub",
            "fingerprint": "fp",
        }
    )
    row = await repo.get(id="default")
    assert row is not None
    assert "jiuwenclaw_id" not in row
    assert await repo.update({"id": "default"}, {"fingerprint": "fp2"}) is not None
    assert (await repo.get(id="default"))["fingerprint"] == "fp2"


@pytest.mark.asyncio
async def test_cron_job_repository_key() -> None:
    store = InMemoryPersistentBackend()
    repo = create_enterprise_record_repository(
        store, "cron_job", instance_id=""
    )
    created = await repo.create(
        {
            "job_id": "job-1",
            "name": "daily",
            "cron_expr": "0 9 * * *",
            "timezone": "UTC",
            "targets": "web",
        }
    )
    assert "jiuwenclaw_id" not in created
    assert created["job_id"] == "job-1"
    assert await repo.get(job_id="job-1") is not None


@pytest.mark.asyncio
async def test_task_memory_single_document_sync() -> None:
    store = InMemoryPersistentBackend()
    repo = EnterpriseRecordRepository(
        store, "task_memory_config", instance_id=""
    )
    await repo.sync_by_business_key([{"enabled": True, "llm_model": "m"}])
    body = await repo.get()
    assert body is not None
    assert body["enabled"] is True
    assert "jiuwenclaw_id" not in body

    await repo.sync_by_business_key([])
    assert await repo.get() is None


@pytest.mark.asyncio
async def test_access_not_injected_by_default() -> None:
    clear_enterprise_record_repositories()
    assert get_enterprise_record_repository("model_template") is None

    store = InMemoryPersistentBackend()
    repos = create_enterprise_record_repositories(
        store, instance_id="inst-1"
    )
    assert set(repos) == set(ENTERPRISE_RECORD_STORE_NAMES)
    set_enterprise_record_repositories(repos)
    assert get_enterprise_record_repository("model_template") is repos["model_template"]
    clear_enterprise_record_repositories()
    assert get_enterprise_record_repository("model_template") is None


def test_layouts_register_all_enterprise_names() -> None:
    registry = build_gateway_store_registry()
    for name in ENTERPRISE_RECORD_STORE_NAMES:
        layout = registry.get(name)
        assert layout.db is not None
        assert layout.db.table == name
        if name == "cron_job":
            continue
        assert layout.file is None
