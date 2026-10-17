# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import asyncio

import pytest
import pytest_asyncio

from jiuwenswarm.server.runtime.marketplace.asset_publish_models import (
    PublishIdentity,
    PublishResult,
)
from jiuwenswarm.server.runtime.marketplace.asset_publish_service import (
    AssetPublishService,
)
from jiuwenswarm.server.runtime.marketplace.asset_publish_store import (
    PublishStore,
    PublishStoreError,
)
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import (
    PublishAuth,
    PublishUploadError,
)


class Publisher:
    def __init__(self, result="pending_moderation", error=None):
        self.result, self.error, self.calls = result, error, []

    async def publish(self, request, *, auth):
        self.calls.append((request, auth))
        await asyncio.sleep(0)
        if self.error:
            raise self.error
        return PublishResult(
            "remote-asset", request.identity, self.result, "public", False
        )


@pytest_asyncio.fixture
async def setup(tmp_path):
    source = tmp_path / "local"
    source.mkdir()
    (source / "SKILL.md").write_text(
        "---\nname: demo\ndescription: example\n---\nbody\n"
    )
    store = PublishStore(tmp_path / "publish.db")
    publisher = Publisher()
    service = AssetPublishService(
        store, publisher, lambda scope, kind, local_id: source, tmp_path / "packages"
    )
    yield service, store, publisher, tmp_path
    await service.close()
    store.close()


async def prepare(service):
    return await service.prepare(
        "trusted-user-workspace-hub",
        "local-resource-id",
        PublishIdentity("skill", "demo", "1.0.0"),
        {},
    )


@pytest.mark.asyncio
async def test_prepare_commit_status_records_no_zip_or_token_in_response(setup):
    service, store, publisher, tmp_path = setup
    draft = await prepare(service)
    assert "artifact_path" not in draft and draft["files"]
    token = "synthetic-private-token"
    op = await service.commit(
        "trusted-user-workspace-hub",
        draft["draft_id"],
        "request-1",
        auth=PublishAuth(token),
    )
    await service.wait_idle()
    record = service.status("trusted-user-workspace-hub", op["operation_id"])
    assert record["execution_status"] == "completed"
    assert record["result"]["publish_result"] == "pending_moderation"
    assert (
        len(service.records("trusted-user-workspace-hub", "skill", "local-resource-id"))
        == 1
    )
    assert len(publisher.calls) == 1
    assert publisher.calls[0][1].access_token == token
    assert token.encode() not in (tmp_path / "publish.db").read_bytes()
    assert not list((tmp_path / "packages").rglob("artifact.zip"))


@pytest.mark.asyncio
async def test_repeat_click_uploads_once_and_cannot_cross_owner(setup):
    service, store, publisher, _ = setup
    draft = await prepare(service)
    auth = PublishAuth("test-token")
    args = ("trusted-user-workspace-hub", draft["draft_id"])
    first, second = await asyncio.gather(
        service.commit(*args, "r1", auth=auth), service.commit(*args, "r2", auth=auth)
    )
    assert first["operation_id"] == second["operation_id"]
    await service.wait_idle()
    assert len(publisher.calls) == 1
    with pytest.raises(PublishStoreError):
        service.status("another-user", first["operation_id"])


@pytest.mark.asyncio
async def test_unknown_upload_never_retries_or_turns_into_success(setup):
    service, _, publisher, _ = setup
    publisher.error = PublishUploadError("upload_outcome_unknown", outcome_unknown=True)
    draft = await prepare(service)
    op = await service.commit(
        "trusted-user-workspace-hub",
        draft["draft_id"],
        "r1",
        auth=PublishAuth("test-token"),
    )
    await service.wait_idle()
    record = service.status("trusted-user-workspace-hub", op["operation_id"])
    assert record["execution_status"] == "unknown"
    assert record["error"]["outcome_unknown"] is True
    assert record["result"] is None
    assert len(publisher.calls) == 1


@pytest.mark.asyncio
async def test_tampered_artifact_fails_before_publisher(setup):
    service, store, publisher, _ = setup
    draft = await prepare(service)
    request = store.get_draft("trusted-user-workspace-hub", draft["draft_id"])
    request.artifact_path.write_bytes(b"tampered")
    op = await service.commit(
        "trusted-user-workspace-hub",
        draft["draft_id"],
        "r1",
        auth=PublishAuth("test-token"),
    )
    await service.wait_idle()
    assert (
        service.status("trusted-user-workspace-hub", op["operation_id"])[
            "execution_status"
        ]
        == "failed"
    )
    assert publisher.calls == []


@pytest.mark.asyncio
async def test_close_immediately_after_commit_marks_queued_failed_and_cleans(setup):
    service, _, publisher, tmp_path = setup
    draft = await prepare(service)
    op = await service.commit(
        "trusted-user-workspace-hub",
        draft["draft_id"],
        "r1",
        auth=PublishAuth("test-token"),
    )
    await service.close()
    assert (
        service.status("trusted-user-workspace-hub", op["operation_id"])[
            "execution_status"
        ]
        == "failed"
    )
    assert publisher.calls == []
    assert not list((tmp_path / "packages").rglob("artifact.zip"))


@pytest.mark.asyncio
async def test_close_uploading_and_waiting_tasks_cleans_both(setup):
    service, _, publisher, tmp_path = setup
    entered = asyncio.Event()

    async def blocking(request, *, auth):
        entered.set()
        await asyncio.Event().wait()

    publisher.publish = blocking
    first = await prepare(service)
    second = await service.prepare(
        "trusted-user-workspace-hub",
        "local-resource-id",
        PublishIdentity("skill", "demo", "2.0.0"),
        {},
    )
    first_op = await service.commit(
        "trusted-user-workspace-hub",
        first["draft_id"],
        "r1",
        auth=PublishAuth("test-token"),
    )
    await entered.wait()
    second_op = await service.commit(
        "trusted-user-workspace-hub",
        second["draft_id"],
        "r2",
        auth=PublishAuth("test-token"),
    )
    await asyncio.sleep(0)
    await service.close()
    assert (
        service.status("trusted-user-workspace-hub", first_op["operation_id"])[
            "execution_status"
        ]
        == "unknown"
    )
    assert (
        service.status("trusted-user-workspace-hub", second_op["operation_id"])[
            "execution_status"
        ]
        == "failed"
    )
    assert not list((tmp_path / "packages").rglob("artifact.zip"))


@pytest.mark.asyncio
async def test_cancelling_prepare_does_not_leave_background_artifact(
    setup, monkeypatch
):
    from jiuwenswarm.server.runtime.marketplace import asset_publish_service as module
    import threading

    service, _, _, tmp_path = setup
    entered, release, finished = threading.Event(), threading.Event(), threading.Event()
    original = module.build_asset_package

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(timeout=5)
        result = original(*args, **kwargs)
        finished.set()
        return result

    monkeypatch.setattr(module, "build_asset_package", delayed)
    task = asyncio.create_task(prepare(service))
    await asyncio.to_thread(entered.wait, 5)
    task.cancel()
    release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await asyncio.to_thread(finished.wait, 5)
    await asyncio.sleep(0)
    assert not list((tmp_path / "packages").rglob("artifact.zip"))


@pytest.mark.asyncio
async def test_start_recovers_once_and_cleans_orphans(setup):
    service, store, _, tmp_path = setup
    orphan = tmp_path / "packages" / "publish-abandoned"
    orphan.mkdir(parents=True)
    (orphan / "artifact.zip").write_bytes(b"orphan")
    await service.start()
    assert not orphan.exists()
    draft = await prepare(service)
    operation, _ = store.commit_draft(
        "trusted-user-workspace-hub", draft["draft_id"], "r1"
    )
    await service.start()
    assert (
        store.status("trusted-user-workspace-hub", operation)["execution_status"]
        == "queued"
    )
    await service.close()


@pytest.mark.asyncio
async def test_uncommitted_drafts_bounded_and_expire(setup):
    service, store, _, _ = setup
    service._max_drafts = 1
    draft = await prepare(service)
    with pytest.raises(PublishStoreError, match="draft_limit"):
        await prepare(service)
    request = store.get_draft("trusted-user-workspace-hub", draft["draft_id"])
    service.cleanup(now=draft["expires_at"] + 1)
    assert not request.artifact_path.exists()
    await service.close()


@pytest.mark.asyncio
async def test_restart_fails_queue_and_retains_unknown_without_upload(setup):
    service, store, publisher, tmp_path = setup
    draft = await prepare(service)
    op, _ = store.commit_draft("trusted-user-workspace-hub", draft["draft_id"], "r1")
    store.start("trusted-user-workspace-hub", op)
    await service.close()
    restarted = AssetPublishService(
        store, publisher, service._resolve_source, tmp_path / "packages"
    )
    await restarted.start()
    assert (
        restarted.status("trusted-user-workspace-hub", op)["execution_status"]
        == "unknown"
    )
    assert not list((tmp_path / "packages").rglob("artifact.zip"))
    assert not publisher.calls
    await restarted.close()


@pytest.mark.asyncio
async def test_disk_limit_removes_failed_build_and_missing_artifact_is_known_failure(
    setup,
):
    service, store, publisher, tmp_path = setup
    service._max_artifact_bytes = 1
    with pytest.raises(PublishStoreError, match="artifact_limit"):
        await prepare(service)
    assert not list((tmp_path / "packages").rglob("artifact.zip"))
    service._max_artifact_bytes = 1024 * 1024
    draft = await prepare(service)
    store.get_draft(
        "trusted-user-workspace-hub", draft["draft_id"]
    ).artifact_path.unlink()
    op = await service.commit(
        "trusted-user-workspace-hub",
        draft["draft_id"],
        "r",
        auth=PublishAuth("test-token"),
    )
    await service.wait_idle()
    record = service.status("trusted-user-workspace-hub", op["operation_id"])
    assert record["execution_status"] == "failed"
    assert record["error"]["code"] == "artifact_unavailable"
    assert not publisher.calls


@pytest.mark.asyncio
async def test_custom_source_and_success_notification_failure_preserves_outcome(setup):
    from jiuwenswarm.server.runtime.marketplace.asset_mcp_publish_converter import (
        CustomMcpSource,
    )

    service, _, publisher, tmp_path = setup
    service._resolve_source = lambda *args: CustomMcpSource(
        {
            "url": "https://example.com/api/v1/mcp",
            "headers": {"Authorization": "Bearer secret"},
        }
    )
    notified = []

    def callback(request, result):
        notified.append(request.identity.kind)
        raise RuntimeError("local cache failure")

    service._on_published = callback
    publisher.result = "published"
    draft = await service.prepare(
        "scope",
        "custom-id",
        PublishIdentity("mcp", "demo", "1.0.0"),
        {"description": "description"},
    )
    assert not list((tmp_path / "packages").glob("mcp-source-*"))
    op = await service.commit(
        "scope", draft["draft_id"], "r", auth=PublishAuth("test-token")
    )
    await service.wait_idle()
    assert notified == ["mcp"]
    assert (
        service.status("scope", op["operation_id"])["execution_status"] == "completed"
    )


@pytest.mark.asyncio
async def test_close_during_prepare_waits_for_thread_and_removes_output(
    setup, monkeypatch
):
    from jiuwenswarm.server.runtime.marketplace import asset_publish_service as module
    import threading

    service, _, _, tmp_path = setup
    entered, release = threading.Event(), threading.Event()
    original = module.build_asset_package

    def delayed(*args, **kwargs):
        entered.set()
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(module, "build_asset_package", delayed)
    preparing = asyncio.create_task(prepare(service))
    assert await asyncio.to_thread(entered.wait, 5)
    closing = asyncio.create_task(service.close())
    await asyncio.sleep(0)
    release.set()
    await closing
    assert preparing.cancelled()
    assert not list((tmp_path / "packages").rglob("artifact.zip"))
    assert not service._tasks and not service._prepares


@pytest.mark.asyncio
async def test_expiry_maintenance_runs_without_more_requests(setup):
    service, store, _, _ = setup
    service._draft_ttl = 0.02
    service._cleanup_interval = 0.01
    draft = await prepare(service)
    path = store.get_draft(
        "trusted-user-workspace-hub", draft["draft_id"]
    ).artifact_path
    async with asyncio.timeout(1):
        while path.exists():
            await asyncio.sleep(0.01)
    assert store.pending_draft_count() == 0


@pytest.mark.asyncio
async def test_custom_prepare_lists_generated_review_changes(setup):
    from jiuwenswarm.server.runtime.marketplace.asset_mcp_publish_converter import (
        CustomMcpSource,
    )

    service, _, _, _ = setup
    service._resolve_source = lambda *args: CustomMcpSource(
        {"url": "https://example.com/mcp"}
    )
    draft = await service.prepare(
        "scope",
        "custom-id",
        PublishIdentity("mcp", "demo", "1.0.0"),
        {"description": "description"},
    )
    assert any("display_name" in change for change in draft["normalizations"])
    assert any("placeholder" in change for change in draft["normalizations"])
