from __future__ import annotations

import pytest

from jiuwenswarm.gateway.cron.etcd_store import EtcdCronJobStore
from jiuwenswarm.gateway.cron.factory import create_gateway_cron_store, load_cron_store_settings
from jiuwenswarm.gateway.cron.store import FileCronJobStore


def _cron_cfg(**kwargs: object) -> dict:
    return {"gateway": {"cron": dict(kwargs)}}


@pytest.mark.asyncio
async def test_factory_default_returns_file_store():
    store = await create_gateway_cron_store({"gateway": {}})
    assert isinstance(store, FileCronJobStore)


@pytest.mark.asyncio
async def test_factory_missing_cron_section_defaults_to_file():
    store = await create_gateway_cron_store({})
    assert isinstance(store, FileCronJobStore)


@pytest.mark.asyncio
async def test_factory_explicit_file_backend():
    store = await create_gateway_cron_store(_cron_cfg(store_backend="file"))
    assert isinstance(store, FileCronJobStore)


@pytest.mark.asyncio
async def test_factory_etcd_without_endpoints_does_not_fallback_to_file():
    store = await create_gateway_cron_store(
        _cron_cfg(store_backend="etcd", etcd_endpoints="")
    )
    assert isinstance(store, EtcdCronJobStore)
    assert not isinstance(store, FileCronJobStore)
    jobs = await store.list_jobs()
    assert jobs == []


def test_load_settings_does_not_infer_etcd_from_endpoints_alone():
    settings = load_cron_store_settings(
        _cron_cfg(
            store_backend="file",
            etcd_endpoints="http://127.0.0.1:2379",
        )
    )
    assert settings.backend == "file"
    assert settings.endpoints == ("http://127.0.0.1:2379",)


def test_load_settings_parses_endpoint_list():
    settings = load_cron_store_settings(
        _cron_cfg(
            store_backend="etcd",
            etcd_endpoints=["http://a:2379", "http://b:2379"],
            etcd_prefix="/jiuwenswarm/cron/jobs/",
        )
    )
    assert settings.backend == "etcd"
    assert settings.endpoints == ("http://a:2379", "http://b:2379")
    assert settings.prefix == "/jiuwenswarm/cron/jobs/"


def test_load_settings_ignores_flat_and_agentos_keys():
    settings = load_cron_store_settings(
        {
            "gateway": {
                "cron_store_backend": "etcd",
                "agentos": {"cron_store_backend": "etcd"},
            }
        }
    )
    assert settings.backend == "file"


@pytest.mark.asyncio
async def test_factory_agentos_router_still_defaults_to_file():
    store = await create_gateway_cron_store(
        {
            "gateway": {
                "agent_client": {"type": "agentos_router"},
                "agentos": {},
            }
        }
    )
    assert isinstance(store, FileCronJobStore)
