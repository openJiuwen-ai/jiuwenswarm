# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Confirmed installation of a thin Pack and exact independent members."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
import time
import uuid
from collections.abc import Callable
from dataclasses import asdict
from functools import wraps
from pathlib import Path
from typing import Any

from filelock import FileLock

from jiuwenswarm.server.runtime.marketplace.hub_client import HubClient
from jiuwenswarm.server.runtime.marketplace.hub_models import HubArtifact
from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import HubPackageDownloader
from jiuwenswarm.server.runtime.skill.archive_store import (
    atomic_write_json,
    compute_content_checksum,
    version_content_dir,
    write_skillhub_first_install_index,
)
from jiuwenswarm.server.runtime.skill.skillpack import (
    SkillPackValidationError,
    _read_skill_document,
    load_skillpack,
)
from jiuwenswarm.server.runtime.skill.skillpack_lock import load_skillpack_lock

PROVENANCE_PATH = ".archive/hub-provenance.json"
_PLAN_TTL_SECONDS = 3600
logger = logging.getLogger(__name__)


def inventory_mutex(skills_dir: Path) -> FileLock:
    """Serialize commits with local import, toggle and uninstall operations."""
    directory = skills_dir / ".skillpack"
    directory.mkdir(parents=True, exist_ok=True)
    return FileLock(str(directory / "inventory.lock"), timeout=0, is_singleton=True)


def inventory_mutation(method):
    """Guard synchronous-in-effect async handlers (no suspension while mutating)."""
    @wraps(method)
    async def guarded(manager, params):
        with inventory_mutex(manager._skills_dir):
            manager.reload_state()
            return await method(manager, params)
    return guarded


def read_hub_provenance(skill_dir: Path) -> dict[str, Any] | None:
    try:
        value = json.loads((skill_dir / PROVENANCE_PATH).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def validate_ordinary_skill(skill_dir: Path, name: str) -> None:
    """Require an actual ordinary root rather than trusting Hub tags."""
    meta, _ = _read_skill_document(skill_dir / "SKILL.md")
    if (
        meta.get("name") != name
        or not isinstance(meta.get("description"), str)
        or not meta["description"].strip()
        or str(meta.get("kind") or "").strip().lower() not in {"", "skill"}
    ):
        raise SkillPackValidationError(f"SkillPack member is not an ordinary Skill: {name}")
    if any(path.is_symlink() for path in skill_dir.rglob("*")):
        raise SkillPackValidationError(f"SkillPack member contains symbolic links: {name}")


def _package_root(extracted: Path) -> Path:
    roots = [path.parent for path in extracted.rglob("SKILL.md")]
    if len(roots) != 1:
        raise SkillPackValidationError("Each Hub artifact must contain exactly one root SKILL.md")
    root = roots[0]
    if any(path.name == ".archive" or path.is_symlink() for path in extracted.rglob("*")):
        raise SkillPackValidationError("Hub artifacts must not contain archives or links")
    return root


def _archive_install(source: Path, destination: Path, record: dict[str, Any]) -> None:
    shutil.copytree(source, destination)
    storage_id = f"ver-{uuid.uuid4().hex[:12]}"
    content = version_content_dir(destination, storage_id)
    content.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, content)
    write_skillhub_first_install_index(
        destination, version=record["version"], asset_id=record["asset_id"],
        storage_id=storage_id, checksum_sha256=compute_content_checksum(source),
    )
    atomic_write_json(destination / PROVENANCE_PATH, record)


class SkillPackInstaller:
    """Stage outside the inventory, then commit under a recoverable local transaction.

    The Hub client must be anonymous: an owner-only version is not distributable.
    The host owns state/index registration; these files are restored on failure.
    """

    def __init__(
        self,
        skills_dir: Path,
        hub_url: str,
        client: HubClient,
        *,
        state_files: tuple[Path, ...] = (),
        downloader: HubPackageDownloader | None = None,
        enabled_for: Callable[[str], bool] = lambda name: True,
    ) -> None:
        self.skills_dir = skills_dir
        self.hub_url = hub_url.rstrip("/")
        self.client = client
        self.downloader = downloader or HubPackageDownloader()
        self.enabled_for = enabled_for
        self.state_files = state_files
        self.work_dir = skills_dir / ".skillpack"
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.mutex = inventory_mutex(skills_dir)

    def recover(self) -> None:
        """Roll back interrupted commits before exposing the local inventory."""
        with self.mutex:
            transactions = self.work_dir / "transactions"
            for directory in sorted(transactions.iterdir() if transactions.exists() else []):
                journal_path = directory / "journal.json"
                if not journal_path.is_file():
                    continue
                journal = json.loads(journal_path.read_text(encoding="utf-8"))
                if journal["state"] == "committing":
                    self._rollback(directory, journal)
                    logger.warning("skillpack.install.recovered transaction=%s", directory.name)
                shutil.rmtree(directory)

    def _rollback(self, transaction: Path, journal: dict[str, Any]) -> None:
        logger.warning(
            "skillpack.install.rollback transaction=%s new_count=%d",
            transaction.name, len(journal["new_names"]),
        )
        for name in journal["new_names"]:
            destination = self.skills_dir / name
            marker = destination / ".archive/install-transaction"
            if destination.exists():
                if not marker.is_file() or marker.read_text() != transaction.name:
                    raise RuntimeError(f"Cannot recover changed SkillPack destination: {name}")
                shutil.rmtree(destination)
        for index, target in enumerate(self.state_files):
            backup = transaction / f"state-{index}"
            if journal["state_existed"][index]:
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_name(f".{target.name}.recover")
                shutil.copyfile(backup, temporary)
                temporary.replace(target)
            else:
                target.unlink(missing_ok=True)

    def _local_action(self, record: dict[str, Any]) -> tuple[str, str]:
        path = self.skills_dir / record["name"]
        if path.is_symlink():
            return "blocked", "本地同名路径是链接"
        if not path.exists():
            return "download", ""
        if any(item.is_symlink() for item in path.rglob("*")):
            return "blocked", "本地技能包含链接文件"
        local = read_hub_provenance(path)
        keys = ("hub_url", "asset_id", "version", "artifact_sha256")
        if not local or any(local.get(key) != record[key] for key in keys):
            return "blocked", "本地同名技能的来源、版本或摘要不一致"
        if local.get("content_sha256") != compute_content_checksum(path):
            return "blocked", "本地技能内容已修改"
        if not self.enabled_for(record["name"]):
            return "blocked", "本地技能已禁用"
        return "reuse", ""

    async def _resolve_member(self, member: dict[str, Any]) -> HubArtifact:
        detail = await self.client.get_version(member["asset_id"], member["version"])
        if detail.plugin_type != "skill" or detail.name != member["name"]:
            raise SkillPackValidationError(f"Hub member is not an ordinary Skill: {member['name']}")
        artifact = await self.client.get_artifact(
            member["asset_id"], version=member["version"], is_cli_download=True,
        )
        if (
            artifact.plugin_type != "skill"
            or artifact.name != member["name"]
            or artifact.checksum_sha256 != member["artifact_sha256"]
        ):
            raise SkillPackValidationError(f"Hub member identity or digest changed: {member['name']}")
        return artifact

    async def prepare(self, artifact: HubArtifact, body: bytes) -> dict[str, Any]:
        """Verify the root and resolve the entire plan without installing anything."""
        self.recover()
        if artifact.plugin_type not in {"skill", "skillpack"}:
            raise SkillPackValidationError("Hub Pack transport type is invalid")
        self.downloader._verify_checksum(body, artifact.checksum_sha256)
        detail = await self.client.get_version(artifact.asset_id, artifact.version)
        if detail.plugin_type != artifact.plugin_type or detail.name != artifact.name:
            raise SkillPackValidationError("Hub Pack version identity mismatch")
        token = uuid.uuid4().hex
        plan_dir = self.work_dir / "plans" / token
        plan_dir.mkdir(parents=True)
        try:
            extracted = plan_dir / "extracted"
            self.downloader.extract_zip(body, extracted)
            root = _package_root(extracted)
            definition = load_skillpack(root)
            lock = load_skillpack_lock(root, artifact.version)
            if artifact.name != definition.name:
                raise SkillPackValidationError("Hub Pack name differs from SKILL.md")
            if {path.name for path in root.iterdir()} != {"SKILL.md", "skillpack.lock.json"}:
                raise SkillPackValidationError("A thin SkillPack contains only SKILL.md and skillpack.lock.json")
            shutil.copytree(root, plan_dir / "root")
            shutil.rmtree(extracted)
            pack = {
                "name": lock.name, "asset_id": artifact.asset_id, "version": lock.version,
                "artifact_sha256": artifact.checksum_sha256, "hub_url": self.hub_url,
                "content_sha256": compute_content_checksum(root if root.exists() else plan_dir / "root"),
                "kind": "skillpack",
            }
            records = [pack]
            for member in lock.members:
                record = asdict(member)
                record.update(hub_url=self.hub_url, kind="skill")
                await self._resolve_member(record)
                records.append(record)
            for record in records:
                record["action"], record["reason"] = self._local_action(record)
                if record["action"] == "reuse" and record["kind"] == "skill":
                    validate_ordinary_skill(self.skills_dir / record["name"], record["name"])
            plan = {"plan_id": token, "hub_url": self.hub_url, "created_at": time.time(), "records": records}
            atomic_write_json(plan_dir / "plan.json", plan)
            logger.info(
                "skillpack.install.prepared plan=%s reuse=%d download=%d blocked=%d",
                token,
                sum(record["action"] == "reuse" for record in records),
                sum(record["action"] == "download" for record in records),
                sum(record["action"] == "blocked" for record in records),
            )
            return self._projection(plan)
        except BaseException:
            shutil.rmtree(plan_dir, ignore_errors=True)
            raise

    @staticmethod
    def _projection(plan: dict[str, Any]) -> dict[str, Any]:
        return {
            "success": True, "confirmation_required": True,
            "plan_id": plan["plan_id"], "skill_type": "skillpack",
            "can_install": all(record["action"] != "blocked" for record in plan["records"]),
            "install_plan": plan["records"],
        }

    def _read_plan(self, token: str) -> tuple[Path, dict[str, Any]]:
        if len(token) != 32 or any(char not in "0123456789abcdef" for char in token):
            raise SkillPackValidationError("Invalid SkillPack install plan")
        directory = self.work_dir / "plans" / token
        plan = json.loads((directory / "plan.json").read_text(encoding="utf-8"))
        if plan["hub_url"] != self.hub_url or time.time() - plan["created_at"] > _PLAN_TTL_SECONDS:
            raise SkillPackValidationError("SkillPack install plan expired; prepare it again")
        return directory, plan

    async def commit(
        self,
        token: str,
        *,
        on_committed: Callable[[list[dict[str, Any]]], None],
        expected_asset_id: str | None = None,
    ) -> dict[str, Any]:
        """Revalidate, stage all missing members, and commit or restore all state."""
        directory, plan = self._read_plan(token)
        records = plan["records"]
        if any(record["action"] == "blocked" for record in records):
            raise SkillPackValidationError("SkillPack installation has blocked dependencies")
        pack = records[0]
        if expected_asset_id is not None and pack["asset_id"] != expected_asset_id:
            raise SkillPackValidationError("SkillPack confirmation asset differs from the prepared plan")
        lock = load_skillpack_lock(directory / "root", pack["version"])
        if compute_content_checksum(directory / "root") != pack["content_sha256"]:
            raise SkillPackValidationError("Staged SkillPack changed")
        if [member.name for member in lock.members] != [record["name"] for record in records[1:]]:
            raise SkillPackValidationError("Staged SkillPack plan changed")
        # Re-query the exact public root and members even when they can be reused.
        root_artifact = await self.client.get_artifact(
            pack["asset_id"], version=pack["version"], is_cli_download=True,
        )
        if (
            root_artifact.checksum_sha256 != pack["artifact_sha256"]
            or root_artifact.name != pack["name"]
            or root_artifact.plugin_type not in {"skill", "skillpack"}
        ):
            raise SkillPackValidationError("Hub Pack artifact changed")
        with tempfile.TemporaryDirectory(prefix="skillpack-stage-", dir=self.work_dir) as temporary:
            staging = Path(temporary)
            for record in records:
                action, _ = self._local_action(record)
                if action != record["action"]:
                    raise SkillPackValidationError("Local inventory changed; prepare installation again")
                artifact = await self._resolve_member(record) if record["kind"] == "skill" else root_artifact
                if action == "reuse":
                    if record["kind"] == "skill":
                        validate_ordinary_skill(self.skills_dir / record["name"], record["name"])
                    continue
                if record["kind"] == "skillpack":
                    source = directory / "root"
                else:
                    extracted = staging / f"extract-{record['name']}"
                    await self.downloader.download_and_extract(artifact, extracted)
                    source = _package_root(extracted)
                    validate_ordinary_skill(source, record["name"])
                record["content_sha256"] = compute_content_checksum(source)
                _archive_install(source, staging / record["name"], record)
            with self.mutex:
                for record in records:
                    if self._local_action(record)[0] != record["action"]:
                        raise SkillPackValidationError("Local inventory changed; prepare installation again")
                transaction = self.work_dir / "transactions" / uuid.uuid4().hex
                transaction.mkdir(parents=True)
                journal = {
                    "state": "committing",
                    # Publish the root last so file-only readers cannot see a
                    # Pack before all of its independent members are present.
                    "new_names": [
                        record["name"] for record in [*records[1:], records[0]]
                        if record["action"] == "download"
                    ],
                    "state_existed": [path.exists() for path in self.state_files],
                }
                for index, path in enumerate(self.state_files):
                    if path.exists():
                        shutil.copyfile(path, transaction / f"state-{index}")
                atomic_write_json(transaction / "journal.json", journal)
                try:
                    for name in journal["new_names"]:
                        (staging / name / ".archive/install-transaction").write_text(transaction.name)
                        (staging / name).replace(self.skills_dir / name)
                    on_committed(records)
                    journal["state"] = "committed"
                    atomic_write_json(transaction / "journal.json", journal)
                except BaseException:
                    self._rollback(transaction, journal)
                    shutil.rmtree(transaction)
                    raise
                shutil.rmtree(transaction)
        shutil.rmtree(directory)
        logger.info("skillpack.install.committed plan=%s member_count=%d", token, len(records) - 1)
        return {"success": True, "skill": {"name": pack["name"], "skill_type": "skillpack"}}
