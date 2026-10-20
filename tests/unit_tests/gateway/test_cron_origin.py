"""Cron origin is granted only by the request that owns the job session."""

import time
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from jiuwenswarm.extensions.registry import ExtensionRegistry
from jiuwenswarm.gateway.cron.controller import CronController, _verified_origin
from jiuwenswarm.gateway.cron.models import CronJob
from jiuwenswarm.gateway.cron.store import CronJobStore
from jiuwenswarm.runtime.cron.cron_job_mutations import apply_cron_job_patch


SESSION = "dingtalk_team_room_user"


def job(**kwargs):
    fields = dict(
        id="job", name="digest", enabled=True, cron_expr="0 0 9 * * ? *",
        timezone="UTC", description="digest", targets="dingtalk", session_id=SESSION,
        created_at=time.time(), updated_at=time.time(),
    )
    return CronJob(**(fields | kwargs))


def test_origin_round_trip_and_malformed_values():
    record = job(origin_channel_id="dingtalk").to_dict()
    assert CronJob.from_dict(record).origin_channel_id == "dingtalk"
    for value in (True, 1, ["dingtalk"], {"channel": "dingtalk"}):
        assert CronJob.from_dict({**record, "origin_channel_id": value}).origin_channel_id == ""


def test_changed_session_clears_origin_but_same_session_keeps_it():
    original = job(origin_channel_id="dingtalk")
    assert apply_cron_job_patch(original, {"session_id": SESSION}).origin_channel_id == "dingtalk"
    assert apply_cron_job_patch(original, {"session_id": "other"}).origin_channel_id == ""


def test_unverified_request_has_no_origin():
    assert _verified_origin("dingtalk", SESSION, SESSION) == "dingtalk"
    assert _verified_origin("web", "web_session", SESSION) == ""
    assert _verified_origin("dingtalk", "", SESSION) == ""


@pytest.mark.asyncio
async def test_gateway_ignores_supplied_origin_and_runs_without_extensions(tmp_path, monkeypatch):
    monkeypatch.setattr(ExtensionRegistry, "_instance", None)
    assert ExtensionRegistry.cron_hooks() == {}
    store = CronJobStore(path=tmp_path / "cron_jobs.json")
    async def allowed(*args):
        return True
    async def reload():
        return None
    controller = CronController(store=store, scheduler=SimpleNamespace(
        project_execution_allowed=allowed, reload=reload,
    ))
    params = {
        "name": "digest", "cron_expr": "0 0 9 * * ? *", "timezone": "UTC",
        "description": "digest", "targets": "dingtalk", "session_id": SESSION,
        "origin_channel_id": "dingtalk",
    }
    created = await controller.create_job(params, request_channel_id="web", request_session_id="web_session")
    assert "origin_channel_id" not in created
    patched = await controller.update_job(created["id"], {"origin_channel_id": "dingtalk"})
    assert "origin_channel_id" not in patched

@pytest.mark.asyncio
async def test_file_and_etcd_stores_restore_origin(tmp_path):
    from tests.unit_tests.gateway.test_cron_etcd_store import _store

    stores = [CronJobStore(path=tmp_path / "jobs.json"), _store()[0]]
    for store in stores:
        created = await store.create_job(
            name="digest", cron_expr="0 0 9 * * ? *", timezone="UTC",
            description="digest", targets="dingtalk", session_id=SESSION,
            origin_channel_id="dingtalk",
        )
        restored = await store.get_job(created.id)
        assert restored.origin_channel_id == "dingtalk"
        changed = await store.update_job(created.id, {"session_id": "other"})
        assert changed.origin_channel_id == ""


@pytest.mark.asyncio
async def test_mutation_hook_failure_does_not_undo_job_write(tmp_path, monkeypatch):
    events = []

    def mutation(job, *, action, request_channel_id):
        events.append((action, request_channel_id, job.origin_channel_id))
        raise RuntimeError("hook failed")

    monkeypatch.setattr(
        ExtensionRegistry,
        "_instance",
        SimpleNamespace(_cron_hooks={"dingtalk": SimpleNamespace(mutation=mutation)}),
    )
    store = CronJobStore(path=tmp_path / "jobs.json")
    scheduler = SimpleNamespace(
        project_execution_allowed=AsyncMock(return_value=True), reload=AsyncMock(),
    )
    controller = CronController(store=store, scheduler=scheduler)
    created = await controller.create_job(
        {
            "name": "digest", "cron_expr": "0 0 9 * * ? *", "timezone": "UTC",
            "description": "digest", "targets": "dingtalk", "session_id": SESSION,
        },
        request_channel_id="dingtalk", request_session_id=SESSION,
    )
    updated = await controller.update_job(created["id"], {"description": "new"})
    assert updated["description"] == "new"
    assert events == [("create", "dingtalk", "dingtalk"), ("update", "", "dingtalk")]


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("origin", "added"),
    [
        ("test", {"from_extension": True}),
        ("other", {"from_extension": True}),
        ("test", {"targets": "bad"}),
        ("test", None),
    ],
)
async def test_run_hook_cannot_replace_core_metadata(tmp_path, monkeypatch, origin, added):
    from tests.unit_tests.gateway.test_cron_scheduler import (
        FakeAgentClient, FakeMessageHandler, _make_scheduler,
    )

    def run_metadata(job):
        if added is None:
            raise RuntimeError("hook failed")
        return added

    monkeypatch.setattr(
        ExtensionRegistry, "_instance",
        SimpleNamespace(_cron_hooks={"test": SimpleNamespace(run_metadata=run_metadata)}),
    )
    store = CronJobStore(path=tmp_path / "jobs.json")
    agent = FakeAgentClient()
    scheduler = _make_scheduler(store, FakeMessageHandler(), agent_client=agent)
    scheduled = job(targets="tui", origin_channel_id=origin)
    await scheduler.on_wake(scheduled, "job:1234")
    await scheduler.run_tasks["job:1234"]
    metadata = next(
        req.channel_context
        for req in [*agent.unary_requests, *agent.stream_requests]
        if "cron" in req.channel_context
    )
    assert metadata["targets"] == "tui"
    assert metadata["cron"]["job_id"] == "job"
    assert (metadata.get("from_extension") is True) is (
        origin == "test" and added == {"from_extension": True}
    )
