# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Behavior contracts for independent members and compatible Pack publication."""

import asyncio
import io
import json
import zipfile
from types import SimpleNamespace

import pytest
import pytest_asyncio

from jiuwenswarm.server.runtime.marketplace.asset_package_builder import build_asset_package
from jiuwenswarm.server.runtime.marketplace.asset_publish_adapters import PublishValidationError
from jiuwenswarm.server.runtime.marketplace.asset_publish_models import PublishIdentity, PublishProtocolError, PublishResult
from jiuwenswarm.server.runtime.marketplace.asset_publish_service import AssetPublishService
from jiuwenswarm.server.runtime.marketplace.asset_publish_store import PublishStore
from jiuwenswarm.server.runtime.marketplace.hub_client import HubNotFoundError
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
