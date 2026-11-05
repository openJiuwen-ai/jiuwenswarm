# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Behavior contracts for independent members and compatible Pack publication."""

import asyncio
import io
import json
import os
import zipfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from jiuwenswarm.server.runtime.marketplace.asset_package_builder import build_asset_package
from jiuwenswarm.server.runtime.marketplace.asset_publish_adapters import PublishValidationError
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity, PublishProtocolError, PublishResult
from jiuwenswarm.server.runtime.marketplace.asset_publish_service import AssetPublishService
from jiuwenswarm.server.runtime.marketplace.asset_publish_store import PublishStore, PublishStoreError
from jiuwenswarm.server.runtime.marketplace.hub_client import HubNotFoundError, HubProtocolError
from jiuwenswarm.server.runtime.marketplace.hub_publish_client import PublishAuth, PublishUploadError
from jiuwenswarm.server.runtime.marketplace.hub_publish_port import parse_publish_result
from jiuwenswarm.server.runtime.marketplace.skillpack_publish_service import SkillPackPublishService

SECTIONS = (
    "When to use", "Do not use", "Required inputs", "Side effects and confirmation",
    "Included Skills", "Execution Process", "Failure handling", "Final output",
)


def pack_document():
    return "---\nname: release-pack\nkind: skillpack\ndescription: Release workflow\nskills: [notes, risk]\n---\n" + "\n".join(
        "## " + section + "\nRelevant workflow information.\n" for section in SECTIONS
    )


class Hub:
    base_url = "https://isolated.example"

    def __init__(self):
        self.calls, self.assets = [], {}
        self.lose_response = False

    async def publish(self, request, *, auth):
        name = request.identity.package_name
        asset_id = "asset_" + name
        self.calls.append((request, request.artifact_path.read_bytes()))
        self.assets[asset_id] = {
            "name": name, "asset_id": asset_id, "version": request.identity.version,
            "plugin_type": "skill", "publish_result": "pending_moderation",
            "artifact_sha256": request.artifact_sha256,
        }
        if self.lose_response:
            self.lose_response = False
            raise PublishUploadError("upload_outcome_unknown", outcome_unknown=True)
        return PublishResult(asset_id, request.identity, "pending_moderation", "public", False)

    async def publisher_id(self, auth):
        return "publisher" if auth.access_token != "another-account" else "someone-else"

    async def find_owned(self, name, publisher_id, auth):
        return [value for value in self.assets.values() if value["name"] == name]

    async def review(self, asset_id, version, auth):
        return self.assets[asset_id]

    async def public_artifact(self, asset_id, version, expected_type):
        value = self.assets[asset_id]
        if value["publish_result"] != "published":
            raise HubNotFoundError("not public")
        assert value["version"] == version and value["plugin_type"] == expected_type
        return value

    async def recover_upload(self, name, version, checksum, publisher_id, auth):
        value = self.assets.get("asset_" + name)
        if value and value["version"] == version and value["artifact_sha256"] == checksum:
            return value
        return None

    async def get_version(self, asset_id, version):
        value = await self.public_artifact(asset_id, version, "skill")
        return SimpleNamespace(name=value["name"], plugin_type=value["plugin_type"])

    async def get_artifact(self, asset_id, *, version, is_cli_download=False):
        from jiuwenswarm.server.runtime.marketplace.hub_models import HubArtifact

        assert is_cli_download is True
        value = await self.public_artifact(asset_id, version, "skill")
        return HubArtifact(
            asset_id=asset_id, plugin_type="skill", name=value["name"], version=version,
            download_url=self.base_url + "/original/" + asset_id,
            checksum_sha256=value["artifact_sha256"],
        )

    async def fetch(self, url):
        asset_id = url.rsplit("/", 1)[-1]
        for request, body in self.calls:
            if "asset_" + request.identity.package_name == asset_id:
                return body
        raise AssertionError("Unknown artifact URL")


@pytest_asyncio.fixture
async def setup(tmp_path, monkeypatch):
    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
    monkeypatch.setenv("SKILLPACK_HUB_DEPLOYMENT_SCOPE", "isolated")
    root = tmp_path / "skills"
    for name in ("notes", "risk", "release-pack"):
        (root / name).mkdir(parents=True)
        text = pack_document() if name == "release-pack" else f"---\nname: {name}\ndescription: ordinary skill\n---\nDo work.\n"
        (root / name / "SKILL.md").write_text(text)
    store = PublishStore(tmp_path / "publish.sqlite")
    hub = Hub()
    resolver = lambda kind, local_id: root / local_id
    service = AssetPublishService(store, hub, lambda scope, kind, local_id: resolver(kind, local_id), tmp_path / "packages")
    packs = SkillPackPublishService(tmp_path / "packs", service, store, resolver, hub)
    # Drive transitions explicitly to assert the review boundary without time-based tests.
    monkeypatch.setattr(packs, "resume", lambda *args: None)
    yield packs, service, store, hub, root
    await packs.close()
    await service.close()
    store.close()


async def prepare(packs):
    return await packs.prepare(
        "scope", "publisher", "release-pack", PublishIdentity("skillpack", "release-pack", "1.0.0"),
        {"visibility": "public", "description": "Release workflow"}, auth=PublishAuth("test-token"),
    )


async def advance(packs, service, operation_id):
    await packs.advance("scope", operation_id, PublishAuth("test-token"))
    await service.wait_idle()


@pytest.mark.parametrize("operation_id, expected", [
    ("skillpack-existing", True), ("ordinary-existing", False), ("", False),
    (None, False), (17, False), ([], False), ({}, False),
])
def test_publication_ownership_accepts_only_pack_operation_ids(operation_id, expected):
    assert SkillPackPublishService.owns(operation_id) is expected


@pytest.mark.parametrize("record", [
    {}, {"result": None}, {"result": {}},
    {"result": {"asset_id": "asset_notes", "kind": "skillpack", "version": "0.9.0"}},
    {"result": {"asset_id": "", "kind": "skill", "version": "0.9.0"}},
])
@pytest.mark.asyncio
async def test_name_collision_requires_record_of_independent_skill(setup, monkeypatch, record):
    packs, service, store, hub, _ = setup
    hub.assets["asset_notes"] = {"asset_id": "asset_notes", "name": "notes", "plugin_type": "skill"}
    original_records = service.records

    def records(scope, kind, local_id):
        if local_id == "notes":
            return [record]
        return original_records(scope, kind, local_id)

    monkeypatch.setattr(service, "records", records)
    draft = await prepare(packs)
    assert not draft["can_submit"]
    assert draft["members"][0]["error"]["code"] == "SKILLPACK_MEMBER_NAME_CONFLICT"
    assert store.pending_draft_count() == 1
    assert not hub.calls


@pytest.mark.parametrize("missing_field", ["artifact_sha256", "name"])
@pytest.mark.asyncio
async def test_reused_member_missing_remote_identity_blocks_without_upload(setup, monkeypatch, missing_field):
    packs, service, _, hub, _ = setup
    first = await prepare(packs)
    await packs.commit("scope", first["draft_id"], "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, first["draft_id"])
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    original_public_artifact = hub.public_artifact

    async def public_artifact(asset_id, version, expected_type):
        result = dict(await original_public_artifact(asset_id, version, expected_type))
        if asset_id == "asset_notes":
            result.pop(missing_field)
        return result

    monkeypatch.setattr(hub, "public_artifact", public_artifact)
    draft = await packs.prepare(
        "scope", "publisher", "release-pack", PublishIdentity("skillpack", "release-pack", "2.0.0"),
        {"visibility": "public"}, auth=PublishAuth("test-token"),
    )
    assert not draft["can_submit"]
    assert draft["members"][0]["error"]["code"] == "SKILLPACK_MEMBER_IDENTITY_CONFLICT"
    assert draft["members"][1]["action"] == "reuse"
    assert len(hub.calls) == 2


@pytest.mark.parametrize("body", ["{broken", "[]", "null", '{}', '{"hub_url": 1}'])
@pytest.mark.asyncio
async def test_invalid_member_provenance_blocks_without_upload(setup, body):
    packs, service, store, hub, root = setup
    archive = root / "notes/.archive"
    archive.mkdir()
    (archive / "hub-provenance.json").write_text(body)
    draft = await prepare(packs)
    assert not draft["can_submit"]
    assert draft["members"][0]["error"]["code"] == "INVALID_MEMBER_PROVENANCE"
    assert not hub.calls


async def _prepare_published_members(setup, source):
    from jiuwenswarm.server.runtime.skill.archive_store import compute_content_checksum

    packs, service, _, hub, root = setup
    first = await prepare(packs)
    await packs.commit("scope", first["draft_id"], "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, first["draft_id"])
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    if source == "provenance":
        member = root / "notes"
        archive = member / ".archive"
        archive.mkdir()
        (archive / "hub-provenance.json").write_text(json.dumps({
            "hub_url": hub.base_url, "asset_id": "asset_notes", "version": "1.0.0",
            "artifact_sha256": hub.assets["asset_notes"]["artifact_sha256"],
            "content_sha256": compute_content_checksum(member),
        }))


async def _prepare_next_pack_version(packs):
    return await packs.prepare(
        "scope", "publisher", "release-pack", PublishIdentity("skillpack", "release-pack", "2.0.0"),
        {"visibility": "public"}, auth=PublishAuth("test-token"),
    )


@pytest.mark.parametrize("source", ["provenance", "record"])
@pytest.mark.parametrize("availability", ["missing", "pending_moderation", "withdrawn"])
@pytest.mark.asyncio
async def test_unavailable_reused_member_blocks_only_that_member(setup, monkeypatch, source, availability):
    packs, _, store, hub, _ = setup
    await _prepare_published_members(setup, source)
    original = hub.public_artifact
    if availability == "missing":
        missing = hub.assets.pop("asset_notes")
    else:
        hub.assets["asset_notes"]["publish_result"] = availability

    async def public_artifact(asset_id, version, expected_type):
        if asset_id not in hub.assets:
            raise HubNotFoundError("not public")
        return await original(asset_id, version, expected_type)

    monkeypatch.setattr(hub, "public_artifact", public_artifact)
    draft = await _prepare_next_pack_version(packs)
    assert not draft["can_submit"]
    assert draft["members"][0] == {
        "name": "notes", "action": "blocked", "error": {"code": "SKILLPACK_MEMBER_UNAVAILABLE"},
    }
    assert draft["members"][1]["action"] == "reuse"
    assert store.pending_draft_count() == 0
    with pytest.raises(PublishValidationError) as error:
        await packs.commit("scope", draft["draft_id"], "blocked", auth=PublishAuth("test-token"))
    assert error.value.code == "SKILLPACK_MEMBERS_BLOCKED"
    assert len(hub.calls) == 2

    if availability == "missing":
        hub.assets["asset_notes"] = missing
    hub.assets["asset_notes"]["publish_result"] = "published"
    refreshed = await _prepare_next_pack_version(packs)
    assert refreshed["can_submit"]
    assert [member["action"] for member in refreshed["members"]] == ["reuse", "reuse"]
    assert len(hub.calls) == 2


@pytest.mark.parametrize("source", ["provenance", "record"])
@pytest.mark.parametrize("failure", [TimeoutError("timeout"), HubProtocolError("HTTP 503")])
@pytest.mark.asyncio
async def test_reused_member_network_failure_is_not_treated_as_unavailable(setup, monkeypatch, source, failure):
    packs, _, store, hub, _ = setup
    await _prepare_published_members(setup, source)
    original = hub.public_artifact

    async def public_artifact(asset_id, version, expected_type):
        if asset_id == "asset_notes":
            raise failure
        return await original(asset_id, version, expected_type)

    monkeypatch.setattr(hub, "public_artifact", public_artifact)
    with pytest.raises(type(failure)) as error:
        await _prepare_next_pack_version(packs)
    assert error.value is failure
    assert store.pending_draft_count() == 0
    assert len(hub.calls) == 2


@pytest.mark.asyncio
async def test_network_checks_stop_and_explicit_retry_keeps_upload_operations(setup, monkeypatch):
    packs, service, store, hub, root = setup
    draft = await prepare(packs)
    operation_id = draft["draft_id"]
    await packs.commit("scope", operation_id, "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, operation_id)
    original = hub.review

    async def offline(*args):
        raise OSError("offline")

    monkeypatch.setattr(hub, "review", offline)
    for _ in range(3):
        await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["stage"] == "blocked"
    assert packs.status("scope", operation_id)["error"]["code"] == "SKILLPACK_HUB_CHECK_FAILED"
    calls = len(hub.calls)
    await advance(packs, service, operation_id)
    assert len(hub.calls) == calls == 2
    monkeypatch.setattr(hub, "review", original)
    await packs.commit("scope", operation_id, "retry", auth=PublishAuth("test-token"))
    await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["stage"] == "waiting_member_review"
    assert packs._load("scope", operation_id)["hub_check_failures"] == 0
    assert len(hub.calls) == calls


@pytest.mark.asyncio
async def test_snapshot_obeys_draft_limit_and_close(setup):
    packs, service, store, hub, root = setup
    service._max_drafts = 1
    identity = PublishIdentity("skill", "notes", "1.0.0")
    draft = await service.prepare_snapshot("scope", "notes", identity, {}, root / "notes")
    assert not service._prepares
    with pytest.raises(PublishStoreError, match="draft_limit"):
        await service.prepare_snapshot("scope", "notes", identity, {}, root / "notes")
    # Discard is scoped, preserves committed drafts, and is idempotent after cleanup.
    assert store.discard_uncommitted_draft("other", draft["draft_id"]) is None
    assert store.get_draft("scope", draft["draft_id"])
    service.discard_uncommitted_draft("scope", draft["draft_id"])
    assert store.discard_uncommitted_draft("scope", draft["draft_id"]) is None
    await service.close()
    with pytest.raises(RuntimeError, match="publisher_closed"):
        await service.prepare_snapshot("scope", "notes", identity, {}, root / "notes")


@pytest.mark.asyncio
async def test_snapshot_obeys_artifact_limit(setup):
    packs, service, store, hub, root = setup
    await service.start()
    service._artifact_root.mkdir(exist_ok=True)
    (service._artifact_root / "retained-artifact").write_bytes(b"123")
    service._max_artifact_bytes = 3
    with pytest.raises(PublishStoreError, match="artifact_limit"):
        await service.prepare_snapshot("scope", "notes", PublishIdentity("skill", "notes", "1.0.0"), {}, root / "notes")
    assert not service._prepares and not service._building


@pytest.mark.asyncio
async def test_snapshot_cancel_tracks_and_cleans_thread_output(setup, monkeypatch):
    import threading

    packs, service, store, hub, root = setup
    started, release = threading.Event(), threading.Event()
    original = service._build

    def slow_build(*args):
        started.set()
        assert release.wait(5)
        return original(*args)

    monkeypatch.setattr(service, "_build", slow_build)
    task = asyncio.create_task(service.prepare_snapshot(
        "scope", "notes", PublishIdentity("skill", "notes", "1.0.0"), {}, root / "notes",
    ))
    try:
        assert await asyncio.to_thread(started.wait, 5)
        assert task in service._prepares
        task.cancel()
    finally:
        release.set()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert not service._prepares and not service._building
    assert store.pending_draft_count() == 0
    assert not list(service._artifact_root.rglob("*.zip"))


@pytest.mark.asyncio
async def test_worker_error_logs_identity_not_raw_exception(setup, caplog):
    packs, *_ = setup

    async def fail():
        raise AttributeError("sensitive request details")

    task = asyncio.create_task(fail())
    await asyncio.gather(task, return_exceptions=True)
    packs._task_done("operation-test", task)
    assert "operation-test" in caplog.text and "AttributeError" in caplog.text
    assert "sensitive request details" not in caplog.text


@pytest.mark.asyncio
async def test_confirmation_publishes_members_then_reviewed_thin_pack(setup):
    packs, service, store, hub, root = setup
    draft = await prepare(packs)
    assert [item["action"] for item in draft["members"]] == ["publish", "publish"]
    assert not hub.calls
    assert not (root / "release-pack" / "skillpack.lock.json").exists()
    first = await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    second = await packs.commit("scope", draft["draft_id"], "second", auth=PublishAuth("test-token"))
    assert first["operation_id"] == second["operation_id"]
    operation_id = first["operation_id"]
    await advance(packs, service, operation_id)
    assert len(hub.calls) == 2
    await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["stage"] == "waiting_member_review"
    hub.assets["asset_notes"]["publish_result"] = "published"
    await advance(packs, service, operation_id)
    assert len(hub.calls) == 2
    hub.assets["asset_risk"]["publish_result"] = "published"
    await advance(packs, service, operation_id)
    assert len(hub.calls) == 3
    request, contents = hub.calls[-1]
    assert request.identity.kind == "skillpack"
    assert request.transport_profile == "legacy_skill_v1"
    assert "openjiuwen-skillpack-v1" in request.tags
    assert "teamskills" not in request.tags
    with zipfile.ZipFile(io.BytesIO(contents)) as archive:
        assert set(archive.namelist()) == {
            "release-pack/plugin.yaml", "release-pack/release-pack/SKILL.md",
            "release-pack/release-pack/skillpack.lock.json",
        }
        lock = json.loads(archive.read("release-pack/release-pack/skillpack.lock.json"))
        assert [item["asset_id"] for item in lock["members"]] == ["asset_notes", "asset_risk"]
        assert lock["members"][0]["artifact_sha256"] == hub.calls[0][0].artifact_sha256
    await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["stage"] == "waiting_pack_review"
    hub.assets["asset_release-pack"]["publish_result"] = "published"
    await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["execution_status"] == "completed"
    await advance(packs, service, operation_id)
    assert len(hub.calls) == 3


@pytest.mark.asyncio
async def test_changed_confirmed_member_blocks_pack_after_independent_upload(setup):
    packs, service, _, hub, root = setup
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    await advance(packs, service, draft["draft_id"])
    (root / "risk" / "SKILL.md").write_text("changed")
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    await advance(packs, service, draft["draft_id"])
    assert len(hub.calls) == 2
    assert packs.status("scope", draft["draft_id"])["error"]["code"] == "SKILLPACK_CONTENT_CHANGED"


@pytest.mark.asyncio
async def test_rejected_member_never_publishes_pack_or_deletes_successful_member(setup):
    packs, service, _, hub, _ = setup
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    await advance(packs, service, draft["draft_id"])
    hub.assets["asset_notes"]["publish_result"] = "publish_failed"
    await advance(packs, service, draft["draft_id"])
    assert packs.status("scope", draft["draft_id"])["stage"] == "blocked"
    assert len(hub.calls) == 2 and "asset_risk" in hub.assets


@pytest.mark.asyncio
async def test_unknown_upload_is_reconciled_without_duplicate_post(setup):
    packs, service, _, hub, _ = setup
    hub.lose_response = True
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    await advance(packs, service, draft["draft_id"])
    await advance(packs, service, draft["draft_id"])
    assert len(hub.calls) == 2
    assert packs.status("scope", draft["draft_id"])["stage"] == "waiting_member_review"


@pytest.mark.asyncio
async def test_nonordinary_member_and_default_public_gate_block(setup, monkeypatch):
    packs, _, _, hub, root = setup
    (root / "risk" / "SKILL.md").write_text("---\nname: risk\nkind: swarm-skill\ndescription: team\n---\n")
    draft = await prepare(packs)
    assert not draft["can_submit"]
    assert draft["members"][1]["action"] == "blocked"
    monkeypatch.delenv("SKILLPACK_HUB_DEPLOYMENT_SCOPE")
    with pytest.raises(PublishValidationError) as error:
        await prepare(packs)
    assert error.value.code == "SKILLPACK_MARKET_GATE_REQUIRED"
    assert not hub.calls


def test_ordinary_adapter_rejects_pack_and_profile_does_not_downgrade(tmp_path):
    source = tmp_path / "pack"
    source.mkdir()
    (source / "SKILL.md").write_text(pack_document())
    with pytest.raises(PublishValidationError, match="SkillPack publishing plan"):
        build_asset_package(source, PublishIdentity("skill", "release-pack", "1.0.0"), {}, tmp_path / "out")
    payload = {"plugin_type": "skill", "asset_type": "plugin", "asset_id": "asset_pack",
               "name": "release-pack", "version": "1.0.0", "visibility": "public"}
    identity = PublishIdentity("skillpack", "release-pack", "1.0.0")
    assert parse_publish_result(payload, identity, transport_profile="legacy_skill_v1").identity.kind == "skillpack"
    with pytest.raises(PublishProtocolError):
        parse_publish_result(payload, identity, transport_profile="native_skillpack_v1")


@pytest.mark.asyncio
async def test_reopen_recovers_review_stage_and_does_not_upload_members_twice(setup):
    packs, service, store, hub, _ = setup
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    await advance(packs, service, draft["draft_id"])
    await advance(packs, service, draft["draft_id"])
    restored = SkillPackPublishService(packs.root, service, store, packs.resolver, hub)
    try:
        assert restored.status("scope", draft["draft_id"])["stage"] == "waiting_member_review"
        for asset in hub.assets.values():
            asset["publish_result"] = "published"
        await advance(restored, service, draft["draft_id"])
        assert [call[0].identity.package_name for call in hub.calls] == ["notes", "risk", "release-pack"]
    finally:
        await restored.close()


@pytest.mark.asyncio
async def test_restart_before_upload_rebuilds_confirmed_bytes_and_resumes(setup):
    packs, service, _, hub, _ = setup
    service._upload_slot = asyncio.Semaphore(0)
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    await packs.advance("scope", draft["draft_id"], PublishAuth("test-token"))
    await service.close()
    assert not hub.calls
    restored_store = PublishStore(packs.root.parent / "publish.sqlite")
    restored_service = AssetPublishService(
        restored_store, hub, lambda scope, kind, local_id: packs.resolver(kind, local_id),
        packs.root.parent / "packages",
    )
    restored = SkillPackPublishService(packs.root, restored_service, restored_store, packs.resolver, hub)
    try:
        for _ in range(4):
            await advance(restored, restored_service, draft["draft_id"])
        assert [call[0].identity.package_name for call in hub.calls] == ["notes", "risk"]
        assert restored.status("scope", draft["draft_id"])["stage"] == "waiting_member_review"
    finally:
        await restored.close()
        await restored_service.close()
        restored_store.close()


@pytest.mark.asyncio
async def test_preflight_replaces_blocked_draft_after_member_is_fixed(setup):
    packs, _, _, _, root = setup
    member = root / "risk" / "SKILL.md"
    original = member.read_text()
    member.write_text(original.replace("name: risk", "name: risk\nkind: swarm-skill"))
    blocked = await prepare(packs)
    assert not blocked["can_submit"]
    member.write_text(original)
    fixed = await prepare(packs)
    assert fixed["can_submit"] and fixed["draft_id"] != blocked["draft_id"]


@pytest.mark.asyncio
async def test_changed_snapshot_requires_new_consent_and_reuses_successful_members(setup):
    packs, service, _, hub, root = setup
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    await advance(packs, service, draft["draft_id"])
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    member = root / "risk" / "SKILL.md"
    member.write_text(member.read_text().replace("name: risk", "name: risk\nversion: 2.0.0"))
    await advance(packs, service, draft["draft_id"])
    assert packs.status("scope", draft["draft_id"])["stage"] == "blocked"
    replanned = await prepare(packs)
    assert replanned["draft_id"] != draft["draft_id"]
    assert replanned["can_submit"]
    assert [item["action"] for item in replanned["members"]] == ["reuse", "publish"]
    assert len(hub.calls) == 2
    assert len(packs.records("scope", "release-pack")) == 1


@pytest.mark.asyncio
async def test_member_provenance_is_reused_only_for_same_hub_and_unchanged_content(setup):
    from jiuwenswarm.server.runtime.skill.archive_store import compute_content_checksum

    packs, _, _, hub, root = setup
    member = root / "notes"
    archive = member / ".archive"
    archive.mkdir()
    hub.assets["original-notes"] = {
        "asset_id": "original-notes", "name": "notes", "version": "2.0.0",
        "plugin_type": "skill", "publish_result": "published", "artifact_sha256": "a" * 64,
    }
    provenance = {
        "hub_url": hub.base_url, "asset_id": "original-notes", "version": "2.0.0",
        "artifact_sha256": "a" * 64, "content_sha256": compute_content_checksum(member),
    }
    path = archive / "hub-provenance.json"
    path.write_text(json.dumps(provenance))
    draft = await prepare(packs)
    assert draft["members"][0]["action"] == "reuse"
    assert draft["members"][0]["asset_id"] == "original-notes"
    provenance["hub_url"] = "https://other.example"
    path.write_text(json.dumps(provenance))
    draft = await prepare(packs)
    assert draft["members"][0]["action"] == "blocked"
    assert draft["members"][0]["error"]["code"] == "SKILLPACK_MEMBER_HUB_CONFLICT"


@pytest.mark.asyncio
async def test_transport_profile_cannot_drift_after_confirmation(setup, monkeypatch):
    packs, service, _, hub, _ = setup
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "first", auth=PublishAuth("test-token"))
    monkeypatch.setenv("SKILLPACK_HUB_TRANSPORT_PROFILE", "native_skillpack_v1")
    await advance(packs, service, draft["draft_id"])
    assert not hub.calls
    assert packs.status("scope", draft["draft_id"])["error"]["code"] == "SKILLPACK_TRANSPORT_CHANGED"


@pytest.mark.asyncio
async def test_api_reuses_same_publish_entry_and_recovers_after_token_refresh(setup, tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.marketplace.asset_publish_api import AssetPublishAPI, PublishAPIError

    _, _, _, hub, root = setup
    api = AssetPublishAPI(
        tmp_path / "api", publisher=hub, skillpack_hub=hub,
        resolver=lambda kind, local_id: root / local_id, hub_url=hub.base_url,
    )
    monkeypatch.setattr(api.skillpacks, "resume", lambda *args: None)
    auth = {"access_token": "test-token", "oauth_provider": "gitcode"}
    reference = {"kind": "skillpack", "local_id": "release-pack", "auth": auth}
    try:
        description = await api.call("describe", reference, gateway_user="local-user")
        draft = await api.call("prepare", dict(reference, metadata=description["defaults"]), gateway_user="local-user")
        operation = await api.call(
            "commit", {"draft_id": draft["draft_id"], "request_id": "r1", "auth": auth}, gateway_user="local-user",
        )
        refreshed = {"access_token": "refreshed-token", "oauth_provider": "gitcode"}
        status = await api.call("status", {"operation_id": operation["operation_id"], "auth": refreshed}, gateway_user="local-user")
        assert status["operation_id"] == operation["operation_id"]
        assert status["kind"] == "skillpack"
        assert "test-token" not in json.dumps(status)
        assert not hub.calls
        with pytest.raises(PublishAPIError):
            await api.call("status", {"operation_id": operation["operation_id"], "auth": refreshed}, gateway_user="other-user")
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_phase_a_publisher_zip_installs_into_another_users_inventory(setup, tmp_path):
    from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import HubPackageDownloader
    from jiuwenswarm.server.runtime.skill.archive_store import compute_content_checksum
    from jiuwenswarm.server.runtime.skill.skillpack import load_skillpack
    from jiuwenswarm.server.runtime.skill.skillpack_install import SkillPackInstaller, read_hub_provenance
    from jiuwenswarm.server.runtime.skill.skillpack_lock import load_skillpack_lock

    packs, service, _, hub, user_a = setup
    draft = await prepare(packs)
    await packs.commit("scope", draft["draft_id"], "publish", auth=PublishAuth("test-token"))
    await advance(packs, service, draft["draft_id"])
    for member in hub.assets.values():
        member["publish_result"] = "published"
    await advance(packs, service, draft["draft_id"])
    hub.assets["asset_release-pack"]["publish_result"] = "published"
    await advance(packs, service, draft["draft_id"])
    assert packs.status("scope", draft["draft_id"])["stage"] == "completed"

    user_b = tmp_path / "user-b" / "skills"
    state = user_b / "registered.json"
    installer = SkillPackInstaller(
        user_b, hub.base_url, hub, state_files=(state,),
        downloader=HubPackageDownloader(fetch_bytes=hub.fetch, allowed_download_hosts=("isolated.example",)),
    )
    artifact = await hub.get_artifact("asset_release-pack", version="1.0.0", is_cli_download=True)
    plan = await installer.prepare(artifact, await hub.fetch(artifact.download_url))
    assert plan["can_install"]
    assert not (user_b / "release-pack").exists()
    assert [item["action"] for item in plan["install_plan"]] == ["download"] * 3
    await installer.commit(
        plan["plan_id"], expected_asset_id="asset_release-pack",
        on_committed=lambda records: state.write_text(json.dumps([record["name"] for record in records])),
    )
    assert json.loads(state.read_text()) == ["release-pack", "notes", "risk"]
    assert load_skillpack(user_b / "release-pack").members == ("notes", "risk")
    lock = load_skillpack_lock(user_b / "release-pack", "1.0.0")
    for member in lock.members:
        record = read_hub_provenance(user_b / member.name)
        assert record["artifact_sha256"] == member.artifact_sha256 == hub.assets[member.asset_id]["artifact_sha256"]
        assert record["content_sha256"] == compute_content_checksum(user_b / member.name)
        assert (user_b / member.name / ".archive/versions/index.json").is_file()
    assert not (user_b / "release-pack" / "notes").exists()
    assert not (user_a / "release-pack" / "skillpack.lock.json").exists()


@pytest.mark.parametrize("stage,code", [
    ("reconcile_upload", "SKILLPACK_UPLOAD_UNCONFIRMED"),
    ("reauth_required", "AUTH_REQUIRED"),
])
@pytest.mark.asyncio
async def test_action_required_is_failed_and_worker_stops(setup, monkeypatch, stage, code):
    packs, service, _, hub, _ = setup
    draft = await prepare(packs)
    operation_id = draft["draft_id"]
    await packs.commit("scope", operation_id, "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, operation_id)
    if stage == "reauth_required":
        from jiuwenswarm.server.runtime.marketplace.hub_client import HubProtocolError

        async def unauthorized(*args):
            error = HubProtocolError("expired")
            error.status_code = 401
            raise error

        monkeypatch.setattr(hub, "review", unauthorized)
    else:
        original_status = service.status
        hub.assets["asset_risk"]["publish_result"] = "published"

        def unknown(scope, op_id):
            original = original_status(scope, op_id)
            if original["result"]["asset_id"] == "asset_notes":
                return dict(original, execution_status="unknown")
            return original

        async def not_found(*args):
            return None

        monkeypatch.setattr(service, "status", unknown)
        monkeypatch.setattr(hub, "recover_upload", not_found)
    await asyncio.wait_for(packs._run("scope", operation_id, PublishAuth("test-token")), timeout=1)
    record = packs.status("scope", operation_id)
    assert record["stage"] == stage and record["execution_status"] == "failed"
    assert record["error"]["code"] == code
    assert len(hub.calls) == 2
    SkillPackPublishService.resume(packs, "scope", operation_id, PublishAuth("test-token"))
    assert not packs._tasks
    retried = await packs.commit("scope", operation_id, "retry", auth=PublishAuth("refreshed-token"))
    assert retried["execution_status"] == "uploading"
    assert retried["stage"] == "publishing_members"
    assert retried["operation_id"] == operation_id and len(hub.calls) == 2


@pytest.mark.asyncio
async def test_review_wait_skips_hashing_and_approved_members_but_revalidates_before_pack(setup, monkeypatch):
    packs, service, _, hub, root = setup
    draft = await prepare(packs)
    operation_id = draft["draft_id"]
    await packs.commit("scope", operation_id, "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, operation_id)
    hub.assets["asset_notes"]["publish_result"] = "published"
    await advance(packs, service, operation_id)
    reviews, public_reads, source_checks = [], [], []
    original_review, original_public, original_check = hub.review, hub.public_artifact, packs._check_sources

    async def review(*args):
        reviews.append(args[0])
        return await original_review(*args)

    async def public(*args):
        public_reads.append(args[0])
        return await original_public(*args)

    def check(data):
        source_checks.append(data["id"])
        return original_check(data)

    monkeypatch.setattr(hub, "review", review)
    monkeypatch.setattr(hub, "public_artifact", public)
    monkeypatch.setattr(packs, "_check_sources", check)
    for _ in range(4):
        await advance(packs, service, operation_id)
    assert reviews == ["asset_risk"] * 4
    assert public_reads == source_checks == []
    # A source mutation cannot slip through the boundary even with unchanged
    # mtime or size: full content hashes are checked before creating the Pack.
    source = root / "notes/SKILL.md"
    stat = source.stat()
    source.write_text(source.read_text().replace("Do work.", "No work."))
    os.utime(source, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    hub.assets["asset_risk"]["publish_result"] = "published"
    await advance(packs, service, operation_id)
    assert len(source_checks) == 1
    assert packs.status("scope", operation_id)["error"]["code"] == "SKILLPACK_CONTENT_CHANGED"
    assert len(hub.calls) == 2


@pytest.mark.asyncio
async def test_reused_member_is_rechecked_before_pack_upload(setup, monkeypatch):
    packs, service, _, hub, _ = setup
    first = await prepare(packs)
    await packs.commit("scope", first["draft_id"], "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, first["draft_id"])
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    # A second Pack version reuses both independently published members.
    second = await packs.prepare(
        "scope", "publisher", "release-pack", PublishIdentity("skillpack", "release-pack", "2.0.0"),
        {"visibility": "public"}, auth=PublishAuth("test-token"),
    )
    assert [member["action"] for member in second["members"]] == ["reuse", "reuse"]
    await packs.commit("scope", second["draft_id"], "confirm2", auth=PublishAuth("test-token"))
    hub.assets["asset_notes"]["artifact_sha256"] = "b" * 64
    await advance(packs, service, second["draft_id"])
    assert packs.status("scope", second["draft_id"])["error"]["code"] == "SKILLPACK_MEMBER_IDENTITY_CONFLICT"
    assert len(hub.calls) == 2


@pytest.mark.asyncio
async def test_review_poll_backoff_is_bounded_and_resets_on_progress(setup, monkeypatch):
    packs, _, _, _, _ = setup
    draft = await prepare(packs)
    operation_id = draft["draft_id"]
    delays = []

    async def progress(*args):
        data = packs._load("scope", operation_id)
        data["stage"] = "waiting_member_review" if len(delays) < 5 else "completed"
        if len(delays) == 4:
            data["members"][0]["review"] = "approved"
        packs._save(data)

    async def sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(packs, "advance", progress)
    monkeypatch.setattr(asyncio, "sleep", sleep)
    await packs._run("scope", operation_id, PublishAuth("test-token"))
    assert delays == [5, 15, 30, 30, 5]


@pytest.mark.asyncio
async def test_pack_review_wait_revalidates_closure_before_completion(setup, monkeypatch):
    packs, service, _, hub, _ = setup
    draft = await prepare(packs)
    operation_id = draft["draft_id"]
    await packs.commit("scope", operation_id, "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, operation_id)
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    await advance(packs, service, operation_id)
    checks = []
    original = hub.public_artifact

    async def public(*args):
        checks.append(args[0])
        return await original(*args)

    def unexpected_hash(*args):
        pytest.fail("Pending Pack review must not hash source directories")

    monkeypatch.setattr(hub, "public_artifact", public)
    monkeypatch.setattr(packs, "_check_sources", unexpected_hash)
    for _ in range(3):
        await advance(packs, service, operation_id)
    assert checks == []
    hub.assets["asset_notes"]["artifact_sha256"] = "b" * 64
    hub.assets["asset_release-pack"]["publish_result"] = "published"
    await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["error"]["code"] == "SKILLPACK_MEMBER_IDENTITY_CONFLICT"
    assert checks == ["asset_release-pack", "asset_notes"]
    assert len(hub.calls) == 3


@pytest.mark.parametrize("stage", ["reconcile_upload", "reauth_required"])
@pytest.mark.asyncio
async def test_status_api_does_not_restart_action_required_worker(setup, tmp_path, monkeypatch, stage):
    from jiuwenswarm.server.runtime.marketplace.asset_publish_api import AssetPublishAPI

    _, _, _, hub, root = setup
    api = AssetPublishAPI(
        tmp_path / "status-api", publisher=hub, skillpack_hub=hub,
        resolver=lambda kind, local_id: root / local_id, hub_url=hub.base_url,
    )
    real_resume = api.skillpacks.resume
    monkeypatch.setattr(api.skillpacks, "resume", lambda *args: None)
    auth = {"access_token": "test-token", "oauth_provider": "gitcode"}
    reference = {"kind": "skillpack", "local_id": "release-pack", "auth": auth}
    try:
        description = await api.call("describe", reference, gateway_user="local")
        draft = await api.call("prepare", dict(reference, metadata=description["defaults"]), gateway_user="local")
        operation = await api.call(
            "commit", {"draft_id": draft["draft_id"], "request_id": "confirm", "auth": auth}, gateway_user="local",
        )
        data = json.loads(api.skillpacks._db.execute("SELECT payload FROM pack_publications").fetchone()[0])
        data["stage"] = stage
        api.skillpacks._save(data)
        monkeypatch.setattr(api.skillpacks, "resume", real_resume)
        status = await api.call("status", {"operation_id": operation["operation_id"], "auth": auth}, gateway_user="local")
        assert status["execution_status"] == "failed" and status["stage"] == stage
        assert not api.skillpacks._tasks and not hub.calls
    finally:
        await api.close()


@pytest.mark.asyncio
async def test_pack_approval_is_not_cached_across_failed_closure_check(setup, monkeypatch):
    packs, service, _, hub, _ = setup
    draft = await prepare(packs)
    operation_id = draft["draft_id"]
    await packs.commit("scope", operation_id, "confirm", auth=PublishAuth("test-token"))
    await advance(packs, service, operation_id)
    for asset in hub.assets.values():
        asset["publish_result"] = "published"
    await advance(packs, service, operation_id)
    hub.assets["asset_release-pack"]["publish_result"] = "published"
    original = hub.public_artifact

    async def transient_failure(*args):
        if args[0] == "asset_notes":
            raise OSError("temporary")
        return await original(*args)

    monkeypatch.setattr(hub, "public_artifact", transient_failure)
    await advance(packs, service, operation_id)
    assert packs._load("scope", operation_id)["pack"]["review"] == "approved"
    assert packs.status("scope", operation_id)["stage"] != "completed"
    monkeypatch.setattr(hub, "public_artifact", original)
    hub.assets["asset_release-pack"]["publish_result"] = "publish_failed"
    await advance(packs, service, operation_id)
    assert packs.status("scope", operation_id)["stage"] == "blocked"
    assert packs.status("scope", operation_id)["error"]["code"] == "SKILLPACK_REVIEW_REJECTED"
    assert len(hub.calls) == 3
