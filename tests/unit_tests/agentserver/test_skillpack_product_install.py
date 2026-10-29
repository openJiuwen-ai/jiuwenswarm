# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Thin Pack installation must not leave a partially usable inventory."""

from __future__ import annotations

import hashlib
import io
import json
import zipfile
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime.marketplace.hub_models import HubArtifact
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import HubPackageDownloader
from jiuwenswarm.server.runtime.skill.skillpack import SkillPackValidationError, load_skillpack
from jiuwenswarm.server.runtime.skill.skillpack_install import SkillPackInstaller, read_hub_provenance
from jiuwenswarm.server.runtime.skill.skillpack_inventory import skillpack_inventory_snapshot
from jiuwenswarm.server.runtime.skill.skillpack_lock import LOCK_SCHEMA, load_skillpack_lock


def _zip(name: str, content: dict[str, str]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(f"{name}/plugin.yaml", "runtime:\n  type: skill\n")
        for filename, text in content.items():
            archive.writestr(f"{name}/{name}/{filename}", text)
    return buffer.getvalue()


def _root(name: str = "release-pack") -> str:
    sections = (
        "When to use", "Do not use", "Required inputs", "Side effects and confirmation",
        "Included Skills", "Execution Process", "Failure handling", "Final output",
    )
    return (
        f"---\nname: {name}\nkind: skillpack\ndescription: release\nskills: [summary, risk]\n---\n"
        + "\n".join(f"## {section}\nDetails for {section}.\n" for section in sections)
    )


class HubFixture:
    def __init__(self) -> None:
        self.bodies = {}
        self.artifacts = {}
        self.requests = []
        self.hidden = set()
        for name in ("summary", "risk"):
            self.add(name, _zip(name, {"SKILL.md": f"---\nname: {name}\ndescription: ordinary\n---\n"}))
        self.lock = {
            "schema_version": LOCK_SCHEMA, "pack": {"name": "release-pack", "version": "1.0.0"},
            "members": [
                {"name": name, "asset_id": name, "version": "1.0.0", "artifact_sha256": self.artifacts[name].checksum_sha256}
                for name in ("summary", "risk")
            ],
        }
        self.build_pack()

    def add(self, name: str, body: bytes) -> None:
        self.bodies[f"https://localhost/{name}.zip"] = body
        self.artifacts[name] = HubArtifact(
            asset_id=name, plugin_type="skill", version="1.0.0", name=name,
            download_url=f"https://localhost/{name}.zip", checksum_sha256=hashlib.sha256(body).hexdigest(),
        )

    def build_pack(self) -> None:
        self.add("release-pack", _zip("release-pack", {"SKILL.md": _root(), "skillpack.lock.json": json.dumps(self.lock)}))

    async def get_version(self, asset_id, version):
        if asset_id in self.hidden:
            raise ValueError("not public/approved")
        artifact = self.artifacts[asset_id]
        assert artifact.version == version
        return SimpleNamespace(plugin_type=artifact.plugin_type, name=artifact.name)

    async def get_artifact(self, asset_id, *, version, is_cli_download=False):
        assert is_cli_download is True
        assert version == self.artifacts[asset_id].version
        self.requests.append((asset_id, version, is_cli_download))
        if asset_id in self.hidden:
            raise ValueError("not public/approved")
        return self.artifacts[asset_id]

    async def fetch(self, url):
        return self.bodies[url]


@pytest.fixture
def environment(tmp_path):
    hub = HubFixture()
    skills = tmp_path / "skills"
    state = skills / "skills_state.json"
    installer = SkillPackInstaller(
        skills, "https://localhost", hub, state_files=(state,),
        downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
    )
    return hub, skills, state, installer


async def _prepare(hub, installer):
    artifact = hub.artifacts["release-pack"]
    return await installer.prepare(artifact, hub.bodies[artifact.download_url])


@pytest.mark.asyncio
async def test_prepare_and_commit_create_independent_archived_members(environment):
    hub, skills, state, installer = environment
    plan = await _prepare(hub, installer)
    assert plan["confirmation_required"] and plan["can_install"]
    assert not (skills / "release-pack").exists()
    assert not state.exists()
    result = await installer.commit(plan["plan_id"], on_committed=lambda records: state.write_text("registered"))
    assert result["success"]
    assert load_skillpack(skills / "release-pack").members == ("summary", "risk")
    assert load_skillpack_lock(skills / "release-pack").members[0].asset_id == "summary"
    for name in ("release-pack", "summary", "risk"):
        assert (skills / name / ".archive/versions/index.json").exists()
        assert read_hub_provenance(skills / name)["artifact_sha256"] == hub.artifacts[name].checksum_sha256
    assert not (skills / "release-pack/summary").exists()
    second = await _prepare(hub, installer)
    assert {record["action"] for record in second["install_plan"]} == {"reuse"}


@pytest.mark.asyncio
async def test_registration_failure_rolls_back_all_directories_and_state(environment):
    hub, skills, state, installer = environment
    state.write_text("before")
    plan = await _prepare(hub, installer)

    def fail(records):
        state.write_text("partial")
        raise RuntimeError("index refresh failed")

    with pytest.raises(RuntimeError, match="index refresh"):
        await installer.commit(plan["plan_id"], on_committed=fail)
    assert state.read_text() == "before"
    assert not any((skills / name).exists() for name in ("release-pack", "summary", "risk"))
    assert (await installer.commit(plan["plan_id"], on_committed=lambda records: None))["success"]


@pytest.mark.asyncio
async def test_member_download_corruption_never_writes_inventory(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    hub.bodies[hub.artifacts["risk"].download_url] += b"changed"
    with pytest.raises(Exception, match="SHA256"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not (skills / "release-pack").exists()
    assert not (skills / "summary").exists()


@pytest.mark.asyncio
async def test_member_removed_after_confirmation_blocks(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    hub.hidden.add("risk")
    with pytest.raises(ValueError, match="approved"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not (skills / "release-pack").exists()


@pytest.mark.asyncio
async def test_local_name_conflict_and_changed_inventory_are_not_overwritten(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    (skills / "risk").mkdir()
    (skills / "risk/SKILL.md").write_text("user content")
    with pytest.raises(ValueError, match="inventory changed"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert (skills / "risk/SKILL.md").read_text() == "user content"
    new_plan = await _prepare(hub, installer)
    assert new_plan["can_install"] is False
    assert new_plan["install_plan"][-1]["action"] == "blocked"


@pytest.mark.asyncio
@pytest.mark.parametrize("kind", ["skillpack", "swarm-skill", "team-skill", "tool"])
async def test_actual_member_root_must_be_ordinary_even_with_hub_skill_type(environment, kind):
    hub, skills, _, installer = environment
    hub.add("risk", _zip("risk", {"SKILL.md": f"---\nname: risk\ndescription: risk\nkind: {kind}\n---\n"}))
    hub.lock["members"][1]["artifact_sha256"] = hub.artifacts["risk"].checksum_sha256
    hub.build_pack()
    plan = await _prepare(hub, installer)
    with pytest.raises(ValueError, match="ordinary"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    assert not (skills / "summary").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("mutation", ["order", "digest", "duplicate", "range", "wildcard", "version"])
async def test_invalid_lock_blocks_before_confirmation(environment, mutation):
    hub, skills, _, installer = environment
    if mutation == "order":
        hub.lock["members"].reverse()
    elif mutation == "digest":
        hub.lock["members"][0]["artifact_sha256"] = "invalid"
    elif mutation == "duplicate":
        hub.lock["members"][1]["asset_id"] = "summary"
    elif mutation == "range":
        hub.lock["members"][0]["version"] = "latest"
    elif mutation == "wildcard":
        hub.lock["members"][0]["version"] = "1.*"
    else:
        hub.lock["pack"]["version"] = "2.0.0"
    hub.build_pack()
    with pytest.raises(ValueError):
        await _prepare(hub, installer)
    assert not (skills / "release-pack").exists()


@pytest.mark.asyncio
async def test_local_edit_changes_inventory_revision_and_prevents_reuse(environment):
    hub, skills, _, installer = environment
    plan = await _prepare(hub, installer)
    await installer.commit(plan["plan_id"], on_committed=lambda records: None)
    before = skillpack_inventory_snapshot(skills, lambda name: True)
    (skills / "risk/SKILL.md").write_text("changed")
    after = skillpack_inventory_snapshot(skills, lambda name: True)
    assert before["inventory_revision"] != after["inventory_revision"]
    assert next(record for record in after["skills"] if record["id"] == "release-pack")["enabled"] is False
    plan = await _prepare(hub, installer)
    assert plan["can_install"] is False


def test_recover_interrupted_transaction_restores_before_state(environment):
    _, skills, state, installer = environment
    transaction = skills / ".skillpack/transactions/interrupted"
    transaction.mkdir(parents=True)
    (transaction / "state-0").write_text("original")
    state.write_text("partial")
    (transaction / "journal.json").write_text(json.dumps({
        "state": "committing", "new_names": ["summary"], "state_existed": [True],
    }))
    (skills / "summary/.archive").mkdir(parents=True)
    (skills / "summary/.archive/install-transaction").write_text("interrupted")
    installer.recover()
    assert state.read_text() == "original"
    assert not (skills / "summary").exists()
    installer.recover()


@pytest.mark.asyncio
async def test_confirmation_cannot_be_used_for_another_asset(environment):
    hub, _, _, installer = environment
    plan = await _prepare(hub, installer)
    with pytest.raises(ValueError, match="confirmation asset"):
        await installer.commit(plan["plan_id"], on_committed=lambda records: None, expected_asset_id="other")


@pytest.mark.asyncio
async def test_manager_marketplace_confirm_and_shared_member_lifecycle(tmp_path, monkeypatch):
    from jiuwenswarm.server.runtime.skill.skill_manager import SkillManager

    monkeypatch.setenv("SKILLPACK_PRODUCTIZATION_ENABLED", "true")
    monkeypatch.setenv("TEAM_SKILLS_HUB_BASE_URL", "https://localhost")
    hub = HubFixture()
    manager = SkillManager(workspace_dir=str(tmp_path))
    monkeypatch.setattr(manager, "_get_mirror_skills_dirs", lambda: [])

    def installer(base_url):
        return SkillPackInstaller(
            manager._skills_dir, base_url, hub,
            state_files=(manager._state_file, tmp_path / "agent-data.json"),
            downloader=HubPackageDownloader(fetch_bytes=hub.fetch),
            enabled_for=manager.get_skill_enabled,
        )

    async def get_data(path, *, params, timeout, base_url):
        assert params["is_cli_download"] is True
        return asdict(hub.artifacts[path.rsplit("/", 1)[-1]])

    async def download(url, *, checksum_sha256):
        return await hub.fetch(url)

    monkeypatch.setattr(manager, "_skillpack_installer", installer)
    monkeypatch.setattr(manager, "_team_skills_hub_http_get_data", get_data)
    monkeypatch.setattr(manager, "_download_zip_and_verify", download)
    params = {"source": "teamskillshub", "identifier": "release-pack"}
    plan = await manager.handle_skills_online_search_install(params)
    assert plan["confirmation_required"]
    assert not (manager._skills_dir / "release-pack").exists()
    assert not (await manager.handle_skills_online_search_install(dict(params, plan_id=plan["plan_id"])))["success"]
    result = await manager.handle_skills_online_search_install(dict(params, plan_id=plan["plan_id"], confirmed=True))
    assert result["success"]
    details = await manager.handle_skills_get({"name": "release-pack"})
    assert details["skill_type"] == "skillpack"
    assert details["skillpack"]["members"][0]["version"] == "1.0.0"
    assert details["enabled"]
    second_lock = dict(hub.lock, pack={"name": "other-pack", "version": "1.0.0"})
    hub.add("other-pack", _zip("other-pack", {"SKILL.md": _root("other-pack"), "skillpack.lock.json": json.dumps(second_lock)}))
    second_params = {"source": "teamskillshub", "identifier": "other-pack"}
    second_plan = await manager.handle_skills_online_search_install(second_params)
    assert [item["action"] for item in second_plan["install_plan"]] == ["download", "reuse", "reuse"]
    await manager.handle_skills_online_search_install(dict(second_params, plan_id=second_plan["plan_id"], confirmed=True))
    assert (await manager.handle_skills_uninstall({"name": "release-pack"}))["success"]
    denied = await manager.handle_skills_uninstall({"name": "summary"})
    assert denied["referencing_skillpacks"] == ["other-pack"]
    assert (await manager.handle_skills_uninstall({"name": "other-pack"}))["success"]
    assert (manager._skills_dir / "summary/SKILL.md").exists()
    assert (await manager.handle_skills_uninstall({"name": "summary"}))["success"]
