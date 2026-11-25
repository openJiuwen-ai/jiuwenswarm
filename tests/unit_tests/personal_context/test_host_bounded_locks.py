"""Host contention must not stall independent reads or stop requests."""

import asyncio
import importlib
from unittest.mock import AsyncMock

import pytest
import yaml

from openjiuwen.harness.personal_context import PersonalContext
from openjiuwen.harness.personal_context.fetch.local_files import LocalFilesFetchService
from jiuwenswarm.server.personal_context import host_api as module


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setattr(module, "get_config", lambda: {})
    monkeypatch.setattr(module, "get_default_models", lambda: [])
    instance = module.PersonalContextHostAPI(home=tmp_path / "pcs")
    yield instance
    instance._personal_context.shutdown()


async def configure(host):
    await host.configure({
        "collection_enabled": False,
        "agent_use_enabled": False,
        "strategy_profile": "rules",
        "fetch_services": [],
    })


@pytest.mark.asyncio
async def test_capacity_write_waits_for_normal_writer_and_preserves_config(host):
    await configure(host)
    before = host._config_path.read_bytes()
    await host._operation_lock.acquire()
    task = asyncio.create_task(host.patch_runtime_config({"max_context_size_mb": 75}))
    try:
        await asyncio.sleep(0.8)
        waiting = not task.done()
        assert host._operation_lock.locked()
        assert host._config_path.read_bytes() == before
        assert host._config.max_context_size_mb == 50
    finally:
        host._operation_lock.release()
    result = await asyncio.wait_for(task, timeout=2)
    assert waiting
    assert result["max_context_size_mb"] == 75
    assert host._config.max_context_size_mb == 75


@pytest.mark.asyncio
async def test_normal_stop_and_existing_agent_switch_finish_in_order(host, monkeypatch):
    await configure(host)
    entered = asyncio.Event()
    original = host._personal_context.stop_collection

    async def stop(*, timeout_seconds):
        entered.set()
        await asyncio.sleep(0.8)
        await original(timeout_seconds=timeout_seconds)

    monkeypatch.setattr(host._personal_context, "stop_collection", stop)
    first = asyncio.create_task(host.set_collection_enabled(False))
    await asyncio.wait_for(entered.wait(), timeout=1)
    try:
        second = await asyncio.wait_for(host.set_agent_use_enabled(True), timeout=2)
        assert second["agent_use_enabled"] is True
        assert yaml.safe_load(host._config_path.read_text(encoding="utf-8"))["agent_use_enabled"] is True
    finally:
        await first


@pytest.mark.asyncio
async def test_cancelled_waiter_does_not_release_another_writer(host):
    await host._operation_lock.acquire()
    task = asyncio.create_task(host.set_agent_use_enabled(True))
    try:
        await asyncio.sleep(0)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host._operation_lock.locked()
    finally:
        host._operation_lock.release()


@pytest.mark.asyncio
async def test_committed_config_reads_do_not_wait_for_writer(host):
    await configure(host)
    await host._operation_lock.acquire()
    try:
        config, overview, services, enabled = await asyncio.wait_for(asyncio.gather(
            host.get_runtime_config(), host.get_overview(), host.list_fetch_services(), host.is_runtime_enabled(),
        ), timeout=0.2)
        assert config["max_context_size_mb"] == 50
        assert overview["config"]["max_context_size_mb"] == 50
        assert services == []
        assert enabled is False
        config["max_context_size_mb"] = 99
        assert host._stored_config["max_context_size_mb"] == 50
    finally:
        host._operation_lock.release()


@pytest.mark.asyncio
async def test_overview_does_not_hold_host_lock_while_core_snapshot_waits(host, monkeypatch):
    await configure(host)
    started = asyncio.Event()
    release = asyncio.Event()
    original = host._personal_context.snapshot

    async def snapshot():
        started.set()
        await release.wait()
        return await original()

    monkeypatch.setattr(host._personal_context, "snapshot", snapshot)
    task = asyncio.create_task(host.get_overview())
    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)
        async with host._operation_lock:
            assert not task.done()
    finally:
        release.set()
        await task


@pytest.mark.asyncio
async def test_one_slow_stop_does_not_queue_other_services(host, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def stop(service_id):
        calls.append(service_id)
        if service_id == "slow":
            started.set()
            await release.wait()

    monkeypatch.setattr(host._personal_context, "stop_fetch_run", stop)
    slow = asyncio.create_task(host.stop_fetch_run("slow"))
    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)
        assert await asyncio.wait_for(host.stop_fetch_run("fast"), timeout=0.2) == {"ok": True}
        assert not slow.done()
        assert calls == ["slow", "fast"]
    finally:
        release.set()
        await slow


@pytest.mark.asyncio
async def test_duplicate_stops_share_task_and_one_cancellation_keeps_other_waiter(host, monkeypatch):
    started = asyncio.Event()
    release = asyncio.Event()
    calls = []

    async def stop(service_id):
        calls.append(service_id)
        started.set()
        await release.wait()

    monkeypatch.setattr(host._personal_context, "stop_fetch_run", stop)
    first = asyncio.create_task(host.stop_fetch_run("notes"))
    second = None
    try:
        await asyncio.wait_for(started.wait(), timeout=0.2)
        second = asyncio.create_task(host.stop_fetch_run("notes"))
        await asyncio.sleep(0)
        first.cancel()
        with pytest.raises(asyncio.CancelledError):
            await first
        assert calls == ["notes"]
        assert not host._fetch_run_stop_tasks["notes"].done()
        release.set()
        assert await asyncio.wait_for(second, timeout=0.2) == {"ok": True}
        assert host._fetch_run_stop_tasks == {}
    finally:
        release.set()
        await asyncio.gather(first, *([second] if second is not None else []), return_exceptions=True)


@pytest.mark.asyncio
async def test_failed_stop_is_reported_and_next_request_can_retry(host, monkeypatch):
    stop = AsyncMock(side_effect=[ValueError("test failure"), None])
    monkeypatch.setattr(host._personal_context, "stop_fetch_run", stop)
    with pytest.raises(ValueError, match="test failure"):
        await host.stop_fetch_run("notes")
    assert host._fetch_run_stop_tasks == {}
    assert await host.stop_fetch_run("notes") == {"ok": True}
    assert stop.await_count == 2


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,field", [("github", "token"), ("gitcode", "pat"), ("feishu", None)])
async def test_slow_authorization_does_not_block_capacity_or_overwrite_new_settings(host, monkeypatch, provider, field):
    await configure(host)
    started = asyncio.Event()
    release = asyncio.Event()

    async def validate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return {"login": "test-account"} if field else {"state": "authorizing"}

    monkeypatch.setattr(module, "_validate_repository_pat_for_write", validate)
    monkeypatch.setattr(host._personal_context, "authorize_provider", validate)
    credentials = {field: "test-credential"} if field else None
    authorization = asyncio.create_task(host.authorize_provider(provider, credentials))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        result = await asyncio.wait_for(host.patch_runtime_config({"max_context_size_mb": 75}), timeout=1)
        assert result["max_context_size_mb"] == 75
        await asyncio.wait_for(host.set_agent_use_enabled(True), timeout=1)
        assert not authorization.done()
    finally:
        release.set()
        await authorization
    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    assert saved["max_context_size_mb"] == host._config.max_context_size_mb == 75
    assert saved["agent_use_enabled"] is True
    if field:
        assert saved["provider_credentials"][provider] == credentials


@pytest.mark.asyncio
@pytest.mark.parametrize("operation", ["authorize", "create"])
async def test_cancelled_pat_validation_leaves_config_unchanged(host, monkeypatch, operation):
    await host.configure({
        "collection_enabled": False, "agent_use_enabled": False,
        "strategy_profile": "rules", "fetch_services": [],
        "provider_credentials": {"github": {"token": "test-credential"}},
    })
    before = host._config_path.read_bytes()
    started = asyncio.Event()

    async def validate(*_args):
        started.set()
        await asyncio.Event().wait()

    monkeypatch.setattr(module, "_validate_repository_pat_for_write", validate)
    task = asyncio.create_task(
        host.authorize_provider("github", {"token": "test-credential"}) if operation == "authorize"
        else host.create_fetch_service({
            "service_id": "repository", "provider": "github", "enabled": False,
            "time_range": {"mode": "all"},
            "source": {"owner": "team", "repo": "project", "resources": ["readme"]},
        })
    )
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert host._config_path.read_bytes() == before
        assert not host._operation_lock.locked()
    finally:
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
@pytest.mark.parametrize("provider,field", [("github", "token"), ("gitcode", "pat")])
async def test_service_creation_validation_keeps_capacity_writes_available(host, monkeypatch, provider, field):
    await host.configure({
        "collection_enabled": False, "agent_use_enabled": False,
        "strategy_profile": "rules", "fetch_services": [],
        "provider_credentials": {provider: {field: "test-credential"}},
    })
    started = asyncio.Event()
    release = asyncio.Event()

    async def validate(*_args):
        started.set()
        await release.wait()
        return {"login": "test-account"}

    monkeypatch.setattr(module, "_validate_repository_pat_for_write", validate)
    creation = asyncio.create_task(host.create_fetch_service({
        "service_id": "repository", "provider": provider, "enabled": False,
        "time_range": {"mode": "all"},
        "source": {"owner": "team", "repo": "project", "resources": ["readme"]},
    }))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        result = await asyncio.wait_for(host.patch_runtime_config({"max_context_size_mb": 75}), timeout=1)
        assert result["max_context_size_mb"] == 75
        assert not creation.done()
    finally:
        release.set()
        await creation
    saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
    assert saved["max_context_size_mb"] == host._config.max_context_size_mb == 75
    assert saved["fetch_services"][0]["credentials"] == {field: "test-credential"}


@pytest.mark.asyncio
async def test_service_creation_rejects_credentials_replaced_during_validation(host, monkeypatch):
    await host.configure({
        "collection_enabled": False, "agent_use_enabled": False,
        "strategy_profile": "rules", "fetch_services": [],
        "provider_credentials": {"github": {"token": "old-test-credential"}},
    })
    started = asyncio.Event()
    release = asyncio.Event()

    async def validate(_provider, credential):
        if credential == "old-test-credential":
            started.set()
            await release.wait()
        return {"login": "test-account"}

    monkeypatch.setattr(module, "_validate_repository_pat_for_write", validate)
    service = {
        "service_id": "repository", "provider": "github", "enabled": False,
        "time_range": {"mode": "all"},
        "source": {"owner": "team", "repo": "project", "resources": ["readme"]},
    }
    creation = asyncio.create_task(host.create_fetch_service(service))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        await host.authorize_provider("github", {"token": "new-test-credential"})
        release.set()
        with pytest.raises(PersonalContext.Error) as error:
            await creation
        assert error.value.status.name == "CONTEXT_PROACTIVE_RUNTIME_TIMEOUT"
        assert error.value.pcs_reason == "timeout"
        saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
        assert saved["fetch_services"] == []
        assert saved["provider_credentials"]["github"] == {"token": "new-test-credential"}
        assert host._config.fetch_services == ()
        await host.create_fetch_service(service)
        saved = yaml.safe_load(host._config_path.read_text(encoding="utf-8"))
        assert saved["fetch_services"][0]["credentials"] == {"token": "new-test-credential"}
    finally:
        release.set()
        await asyncio.gather(creation, return_exceptions=True)


@pytest.mark.asyncio
async def test_real_core_authorization_keeps_settings_available_during_cli_wait(host, monkeypatch):
    await configure(host)
    started = asyncio.Event()
    release = asyncio.Event()
    core_module = importlib.import_module("openjiuwen.harness.personal_context.personal_context")

    async def cli_status(scopes):
        started.set()
        await release.wait()
        return True, set(scopes), True

    monkeypatch.setattr(core_module, "_lark_cli_auth_status", cli_status)
    authorization = asyncio.create_task(host.authorize_provider("feishu"))
    try:
        await asyncio.wait_for(started.wait(), timeout=1)
        result = await asyncio.wait_for(host.patch_runtime_config({"max_context_size_mb": 75}), timeout=1)
        assert result["max_context_size_mb"] == 75
        await asyncio.wait_for(host.set_agent_use_enabled(True), timeout=1)
        assert not authorization.done()
    finally:
        release.set()
        result = await authorization
    assert result["state"] == "authorized"
    assert host._config.max_context_size_mb == 75
    assert host._config.agent_use_enabled is True


@pytest.mark.asyncio
async def test_real_core_fetch_acceptance_releases_host_lock_before_collection_finishes(host, tmp_path, monkeypatch):
    source = tmp_path / "source"
    source.mkdir()
    (source / "note.md").write_text("# Notes\n\nPersonal context collection regression.\n", encoding="utf-8")
    await host.configure({
        "collection_enabled": True, "agent_use_enabled": False, "strategy_profile": "rules",
        "fetch_services": [{
            "service_id": "local-notes", "provider": "local_files", "enabled": False,
            "time_range": {"mode": "all"}, "source": {"root_dir": str(source)}, "credentials": {},
        }],
    })
    started = asyncio.Event()
    release = asyncio.Event()
    original = LocalFilesFetchService.fetch

    async def fetch(self, **kwargs):
        started.set()
        await release.wait()
        async for batch in original(self, **kwargs):
            yield batch

    monkeypatch.setattr(LocalFilesFetchService, "fetch", fetch)
    try:
        accepted = await asyncio.wait_for(host.run_fetch(service_id="local-notes"), timeout=2)
        await asyncio.wait_for(started.wait(), timeout=2)
        assert accepted["state"] == "accepted"
        assert not host._operation_lock.locked()
        assert (await host.get_fetch_run_status("local-notes", run_id=accepted["runs"][0]["run_id"]))["run_state"] == "running"
        updated = await asyncio.wait_for(host.patch_runtime_config({"max_context_size_mb": 75}), timeout=2)
        assert updated["max_context_size_mb"] == 75
        assert (await host.get_fetch_run_status("local-notes", run_id=accepted["runs"][0]["run_id"]))["run_state"] == "running"
        assert yaml.safe_load(host._config_path.read_text(encoding="utf-8"))["max_context_size_mb"] == 75
    finally:
        release.set()
        await host.stop(timeout_seconds=5)
