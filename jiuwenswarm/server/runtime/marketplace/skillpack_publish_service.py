# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Durable, once-confirmed publishing of independent members followed by a thin Pack."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import sqlite3
import tempfile
import time
import uuid
from pathlib import Path
from typing import Callable

import yaml

from ..skill.archive_store import compute_content_checksum
from ..skill.skillpack import load_skillpack
from ..skill.skill_type import resolve_skillpack_member_type
from ..skill.skillpack_lock import SkillPackLock, SkillPackLockedMember, SKILLPACK_HUB_TAG
from .asset_package_builder import collect_publish_files
from .asset_publish_adapters import PublishValidationError, normalize_and_validate
from .asset_publish_models import PublishIdentity
from .asset_publish_service import AssetPublishService
from .asset_publish_store import PublishStore, PublishStoreError
from .hub_client import HubNotFoundError, HubProtocolError
from .hub_publish_client import PublishAuth
from .skillpack_publish_config import skillpack_publish_profile
from .skillpack_publish_hub import SkillPackPublishHub


logger = logging.getLogger(__name__)


def _source_fingerprint(source: Path) -> str:
    # Reject links and special files before computing the shared installed-content
    # checksum, which intentionally ignores archive/cache files.
    collect_publish_files(source)
    return compute_content_checksum(source)


class SkillPackPublishService:
    """Persist progress, never credentials; resume with the authenticated publisher.

    Ordinary uploads retain their own single-attempt durable operation IDs. A
    lost response is reconciled against Hub name/version/checksum before this
    coordinator can proceed. It never resends an uncertain upload.
    """

    def __init__(
        self,
        root: Path,
        service: AssetPublishService,
        store: PublishStore,
        resolver: Callable,
        hub: SkillPackPublishHub,
        *,
        poll_interval: float = 5,
    ):
        self.root = root
        root.mkdir(parents=True, exist_ok=True)
        self.service, self.store, self.resolver, self.hub = service, store, resolver, hub
        self._db = sqlite3.connect(root / "skillpack-publish.sqlite", isolation_level=None)
        self._db.row_factory = sqlite3.Row
        self._db.execute("PRAGMA journal_mode=WAL")
        self._db.executescript("""
            CREATE TABLE IF NOT EXISTS pack_publications (
                id TEXT PRIMARY KEY, scope TEXT NOT NULL, local_id TEXT NOT NULL,
                package_name TEXT NOT NULL, version TEXT NOT NULL,
                payload TEXT NOT NULL, updated_at REAL NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS pack_publication_identity
                ON pack_publications(scope, package_name, version);
        """)
        self._lock = asyncio.Lock()
        self._tasks: dict[str, asyncio.Task] = {}
        self._poll_interval = poll_interval

    def _load(self, scope: str, operation_id: str) -> dict:
        row = self._db.execute(
            "SELECT payload FROM pack_publications WHERE scope=? AND id=?", (scope, operation_id)
        ).fetchone()
        if row is None:
            raise PublishStoreError("invalid_identifier")
        return json.loads(row["payload"])

    def _save(self, data: dict) -> None:
        previous = self._load(data["scope"], data["id"])
        data["updated_at"] = time.time()
        self._db.execute(
            "UPDATE pack_publications SET payload=?, updated_at=? WHERE scope=? AND id=?",
            (json.dumps(data, ensure_ascii=False), data["updated_at"], data["scope"], data["id"]),
        )
        if previous.get("stage") != data.get("stage"):
            logger.info("SkillPack publish transition operation_id=%s stage=%s", data["id"], data["stage"])

    def owns(self, operation_id: object) -> bool:
        return isinstance(operation_id, str) and operation_id.startswith("skillpack-")

    @staticmethod
    def _type(profile: str) -> str:
        return "skill" if profile == "legacy_skill_v1" else "skillpack"

    def _validate_profile(self, data: dict) -> None:
        if skillpack_publish_profile() != data["transport_profile"]:
            raise PublishValidationError("SKILLPACK_TRANSPORT_CHANGED")

    def _check_sources(self, data: dict) -> None:
        for item in [data["pack"], *data["members"]]:
            if item.get("action") == "blocked":
                continue
            source = self.resolver(item["kind"], item["local_id"])
            if _source_fingerprint(source) != item["content_sha256"]:
                raise PublishValidationError("SKILLPACK_CONTENT_CHANGED")
            if item["kind"] == "skill" and resolve_skillpack_member_type(source) != "skill":
                raise PublishValidationError("SKILLPACK_MEMBER_TYPE_INVALID")

    async def prepare(
        self, scope: str, publisher_id: str, local_id: str, identity: PublishIdentity,
        metadata: dict, *, auth: PublishAuth, member_record_scope: str | None = None,
    ) -> dict:
        async with self._lock:
            return await self._prepare(
                scope, publisher_id, local_id, identity, metadata,
                auth=auth, member_record_scope=member_record_scope,
            )

    async def _prepare(
        self, scope: str, publisher_id: str, local_id: str, identity: PublishIdentity,
        metadata: dict, *, auth: PublishAuth, member_record_scope: str | None = None,
    ) -> dict:
        profile = skillpack_publish_profile()
        if metadata.get("visibility", "public") != "public" or metadata.get("force"):
            raise PublishValidationError("SKILLPACK_REQUIRES_IMMUTABLE_PUBLIC_VERSION")
        source = self.resolver("skillpack", local_id)
        definition = load_skillpack(source, expected_name=local_id)
        if len(definition.members) > 32:
            raise PublishValidationError("SKILLPACK_MEMBER_LIMIT")
        if any(path.name not in {"SKILL.md", "skillpack.lock.json", ".archive"} for path in source.iterdir()):
            raise PublishValidationError("PACK_MUST_BE_THIN")
        fingerprint = _source_fingerprint(source)
        root_document = (source / "SKILL.md").read_text(encoding="utf-8")
        if _source_fingerprint(source) != fingerprint:
            raise PublishValidationError("SKILLPACK_CONTENT_CHANGED")
        # Validate root metadata and references before any member is uploaded.
        # These placeholder identities exist only in the private preflight copy;
        # the distribution lock is built from approved Hub results later.
        with tempfile.TemporaryDirectory(prefix="skillpack-check-", dir=self.root) as directory:
            check_root = Path(directory)
            (check_root / "SKILL.md").write_text(root_document, encoding="utf-8")
            preflight_lock = SkillPackLock(identity.package_name, identity.version, tuple(
                SkillPackLockedMember(name, "preflight_" + str(index), "1.0.0", "0" * 64)
                for index, name in enumerate(definition.members)
            ))
            (check_root / "skillpack.lock.json").write_text(json.dumps(preflight_lock.to_dict()), encoding="utf-8")
            normalize_and_validate(check_root, identity, metadata)
        existing = self._db.execute(
            "SELECT payload FROM pack_publications WHERE scope=? AND package_name=? AND version=?",
            (scope, identity.package_name, identity.version),
        ).fetchone()
        if existing:
            data = json.loads(existing["payload"])
            self._validate_profile(data)
            replace_changed_plan = (
                data["confirmed"] and data["stage"] == "blocked"
                and (data.get("error") or {}).get("code") == "SKILLPACK_CONTENT_CHANGED"
                and not data["pack"].get("operation_id")
            )
            if data["confirmed"] and not replace_changed_plan:
                if data["pack"]["content_sha256"] != fingerprint or data["metadata"] != metadata:
                    raise PublishValidationError("SKILLPACK_VERSION_CONFLICT")
                self._check_sources(data)
                return self._draft(data)
            # A new check replaces an unconfirmed plan, allowing the user to fix
            # a blocked member without changing an unpublished Pack version.
            for member in data["members"]:
                if member.get("draft_id"):
                    self.service.discard_uncommitted_draft(scope, member["draft_id"])
            if replace_changed_plan:
                # Keep all already-published member outcomes in history while
                # freeing this not-yet-uploaded Pack version for a fresh consent.
                self._db.execute(
                    "UPDATE pack_publications SET version=? WHERE scope=? AND id=?",
                    (data["pack"]["version"] + ":superseded:" + data["id"], scope, data["id"]),
                )
            else:
                self._db.execute("DELETE FROM pack_publications WHERE scope=? AND id=?", (scope, data["id"]))

        history = self.records(scope, local_id)
        previous = [row for row in history if row.get("result") and row["result"].get("asset_id")]
        target = identity.target_asset_id
        if previous:
            last = self._load(scope, previous[0]["operation_id"])
            if last["transport_profile"] != profile:
                raise PublishValidationError("SKILLPACK_TRANSPORT_CHANGED")
            previous_id = last["pack"].get("asset_id")
            if target is not None and target != previous_id:
                raise PublishValidationError("SKILLPACK_TARGET_CONFLICT")
            target = previous_id
        elif target:
            raise PublishValidationError("SKILLPACK_TARGET_REQUIRES_LOCAL_RECORD")
        collisions = await self.hub.find_owned(identity.package_name, publisher_id, auth)
        if any(item.get("asset_id") != target for item in collisions):
            raise PublishValidationError("SKILLPACK_NAME_CONFLICT")
        if target and (len(collisions) != 1 or collisions[0].get("plugin_type") != self._type(profile)):
            raise PublishValidationError("SKILLPACK_TARGET_CONFLICT")

        data = {
            "id": "skillpack-" + uuid.uuid4().hex,
            "scope": scope, "publisher_id": publisher_id, "local_id": local_id,
            "transport_profile": profile, "hub_origin": self.hub.base_url,
            "metadata": metadata, "confirmed": False, "stage": "prepared",
            "expires_at": time.time() + 1800, "error": None,
            "pack": {
                "kind": "skillpack", "local_id": local_id,
                "name": identity.package_name, "version": identity.version,
                "content_sha256": fingerprint, "target_asset_id": target,
                "document": root_document,
            },
            "members": [],
        }
        for name in definition.members:
            try:
                member = await self._prepare_member(scope, publisher_id, name, auth, member_record_scope)
            except (PublishValidationError, PublishStoreError) as exc:
                member = {"name": name, "action": "blocked", "error": {"code": exc.code}}
            data["members"].append(member)
        self._db.execute(
            "INSERT INTO pack_publications VALUES (?, ?, ?, ?, ?, ?, ?)",
            (data["id"], scope, local_id, identity.package_name, identity.version,
             json.dumps(data, ensure_ascii=False), time.time()),
        )
        return self._draft(data)

    async def _prepare_member(
        self, scope: str, publisher_id: str, name: str, auth: PublishAuth,
        member_record_scope: str | None = None,
    ) -> dict:
        source = self.resolver("skill", name)
        if resolve_skillpack_member_type(source) != "skill":
            raise PublishValidationError("SKILLPACK_MEMBER_TYPE_INVALID")
        fingerprint = _source_fingerprint(source)
        item = {
            "kind": "skill", "local_id": name, "name": name,
            "content_sha256": fingerprint, "visibility": "public", "action": "publish",
        }
        provenance_path = source / ".archive" / "hub-provenance.json"
        if provenance_path.exists():
            if provenance_path.is_symlink():
                raise PublishValidationError("INVALID_MEMBER_PROVENANCE")
            provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
            if provenance.get("hub_url", "").rstrip("/") != self.hub.base_url.rstrip("/"):
                raise PublishValidationError("SKILLPACK_MEMBER_HUB_CONFLICT")
            if provenance.get("content_sha256") != fingerprint:
                raise PublishValidationError("SKILLPACK_MEMBER_LOCALLY_MODIFIED")
            remote = await self.hub.public_artifact(provenance["asset_id"], provenance["version"], "skill")
            if remote["artifact_sha256"] != provenance.get("artifact_sha256") or remote["name"] != name:
                raise PublishValidationError("SKILLPACK_MEMBER_IDENTITY_CONFLICT")
            return dict(item, **remote, action="reuse", review="approved")

        text = (source / "SKILL.md").read_text(encoding="utf-8")
        match = re.match(r"\s*---\s*\n(.*?)\n---(?:\s*\n|$)", text, re.S)
        front = yaml.safe_load(match.group(1)) if match else {}
        if not isinstance(front, dict) or front.get("name") != name:
            raise PublishValidationError("SKILLPACK_MEMBER_IDENTITY_CONFLICT")
        version = str(front.get("version") or "1.0.0")
        identity = PublishIdentity("skill", name, version)
        prepared = await self.service.prepare(scope, name, identity, {"visibility": "public"})
        if _source_fingerprint(source) != fingerprint:
            self.service.discard_uncommitted_draft(scope, prepared["draft_id"])
            raise PublishValidationError("SKILLPACK_CONTENT_CHANGED")
        item.update(version=version, draft_id=prepared["draft_id"], artifact_sha256=prepared["checksum_sha256"])
        # Previously published local members may be reused only when the exact
        # normalized upload still matches, never merely because names match.
        records = [(scope, record) for record in self.service.records(scope, "skill", name)]
        if member_record_scope and member_record_scope != scope:
            records.extend((member_record_scope, record) for record in self.service.records(member_record_scope, "skill", name))
        for record_scope, record in records:
            result = record.get("result") or {}
            if result.get("version") != version or not result.get("asset_id"):
                continue
            request = self.store.operation_request(record_scope, record["operation_id"])
            if request.artifact_sha256 != item["artifact_sha256"]:
                continue
            remote = await self.hub.public_artifact(result["asset_id"], version, "skill")
            if remote["artifact_sha256"] != item["artifact_sha256"] or remote["name"] != name:
                raise PublishValidationError("SKILLPACK_MEMBER_IDENTITY_CONFLICT")
            self.service.discard_uncommitted_draft(scope, item.pop("draft_id"))
            return dict(item, **remote, action="reuse", review="approved")
        collisions = await self.hub.find_owned(name, publisher_id, auth)
        if collisions:
            trusted_ids = {
                record["result"]["asset_id"] for _, record in records
                if record.get("result") and record["result"].get("asset_id")
                and record["result"].get("kind") == "skill"
            }
            target = collisions[0].get("asset_id") if len(collisions) == 1 else None
            used_version = any(
                (record.get("result") or {}).get("asset_id") == target and record.get("version") == version
                for _, record in records
            )
            if target not in trusted_ids or used_version or collisions[0].get("plugin_type") != "skill":
                self.service.discard_uncommitted_draft(scope, item["draft_id"])
                raise PublishValidationError("SKILLPACK_MEMBER_NAME_CONFLICT")
            self.service.discard_uncommitted_draft(scope, item["draft_id"])
            updated = await self.service.prepare(
                scope, name, PublishIdentity("skill", name, version, target), {"visibility": "public"},
            )
            item["draft_id"], item["artifact_sha256"] = updated["draft_id"], updated["checksum_sha256"]
        return item

    @staticmethod
    def _member_view(item: dict) -> dict:
        fields = ("name", "version", "action", "visibility", "asset_id", "review", "error")
        return {key: item[key] for key in fields if key in item}

    def _draft(self, data: dict) -> dict:
        blocked = any(member["action"] == "blocked" for member in data["members"])
        return {
            "draft_id": data["id"], "expires_at": data["expires_at"], "kind": "skillpack",
            "package_name": data["pack"]["name"], "version": data["pack"]["version"],
            "transport_profile": data["transport_profile"], "hub_plugin_type": self._type(data["transport_profile"]),
            "size_bytes": 0, "files": ["SKILL.md", "skillpack.lock.json"], "excluded": [],
            "normalizations": [], "dependencies": [], "warnings": [],
            "members": [self._member_view(item) for item in data["members"]],
            "can_submit": not blocked,
            "errors": [{"code": "SKILLPACK_MEMBERS_BLOCKED"}] if blocked else [],
        }

    def status(self, scope: str, operation_id: str) -> dict:
        data = self._load(scope, operation_id)
        stage = data["stage"]
        status = "completed" if stage == "completed" else "failed" if stage == "blocked" else "uploading"
        pack = data["pack"]
        return {
            "operation_id": operation_id, "draft_id": operation_id,
            "kind": "skillpack", "local_id": data["local_id"],
            "package_name": pack["name"], "version": pack["version"],
            "execution_status": status, "stage": stage,
            "members": [self._member_view(item) for item in data["members"]],
            "result": {
                "asset_id": pack.get("asset_id"), "version": pack["version"],
                "publish_result": "published" if stage == "completed" else "pending_moderation",
                "local_kind": "skillpack", "hub_plugin_type": self._type(data["transport_profile"]),
                "hub_origin": data["hub_origin"], "pack_artifact_sha256": pack.get("artifact_sha256"),
            },
            "error": data.get("error"), "updated_at": data.get("updated_at"),
        }

    def records(self, scope: str, local_id: str) -> list[dict]:
        rows = self._db.execute(
            "SELECT id, payload FROM pack_publications WHERE scope=? AND local_id=? ORDER BY updated_at DESC",
            (scope, local_id),
        ).fetchall()
        return [self.status(scope, row["id"]) for row in rows if json.loads(row["payload"])["confirmed"]]

    async def commit(self, scope: str, draft_id: str, request_id: str, *, auth: PublishAuth) -> dict:
        if not isinstance(request_id, str) or not request_id or len(request_id) > 256:
            raise PublishStoreError("invalid_identifier")
        async with self._lock:
            data = self._load(scope, draft_id)
            self._validate_profile(data)
            if not data["confirmed"]:
                if time.time() > data["expires_at"]:
                    raise PublishStoreError("draft_expired")
                if any(item["action"] == "blocked" for item in data["members"]):
                    raise PublishValidationError("SKILLPACK_MEMBERS_BLOCKED")
                self._check_sources(data)
                data["confirmed"], data["stage"] = True, "publishing_members"
                self._save(data)
        self.resume(scope, draft_id, auth)
        return self.status(scope, draft_id)

    def resume(self, scope: str, operation_id: str, auth: PublishAuth) -> None:
        data = self._load(scope, operation_id)
        if not data["confirmed"] or data["stage"] in {"completed", "blocked"}:
            return
        if operation_id not in self._tasks:
            task = asyncio.create_task(self._run(scope, operation_id, auth))
            self._tasks[operation_id] = task
            task.add_done_callback(lambda done: self._task_done(operation_id, done))

    def _task_done(self, operation_id: str, task: asyncio.Task) -> None:
        self._tasks.pop(operation_id, None)
        if not task.cancelled():
            task.exception()

    async def _run(self, scope: str, operation_id: str, auth: PublishAuth) -> None:
        while True:
            await self.advance(scope, operation_id, auth)
            state = self._load(scope, operation_id)
            if state["stage"] in {"completed", "blocked", "reconcile_upload", "reauth_required"}:
                return
            await asyncio.sleep(self._poll_interval)

    async def advance(self, scope: str, operation_id: str, auth: PublishAuth) -> None:
        """Advance only confirmed, unchanged snapshots; persist before each transition."""
        async with self._lock:
            data = self._load(scope, operation_id)
            if not data["confirmed"] or data["stage"] in {"completed", "blocked"}:
                return
            try:
                self._validate_profile(data)
                self._check_sources(data)
                # Submit every confirmed local member before waiting for any
                # review; a long review must not expire another member's draft.
                for member in data["members"]:
                    if member["action"] == "publish" and not member.get("operation_id"):
                        operation = await self.service.commit(
                            scope, member["draft_id"], self._request_id(data, member), auth=auth,
                        )
                        member["operation_id"] = operation["operation_id"]
                        self._save(data)
                members_ready = True
                pending_stage = None
                for member in data["members"]:
                    if member["action"] == "reuse":
                        remote = await self.hub.public_artifact(member["asset_id"], member["version"], "skill")
                        if remote["artifact_sha256"] != member["artifact_sha256"]:
                            raise PublishValidationError("SKILLPACK_MEMBER_IDENTITY_CONFLICT")
                    elif not await self._advance_upload(data, member, auth):
                        members_ready = False
                        if pending_stage != "reconcile_upload":
                            pending_stage = data["stage"]
                if not members_ready:
                    data["stage"] = pending_stage or "waiting_member_review"
                    self._save(data)
                    return
                # All members are publicly approved. Recheck the source snapshot
                # again before building the root artifact and exact lock.
                self._check_sources(data)
                if "draft_id" not in data["pack"]:
                    await self._prepare_pack(data)
                    self._save(data)
                if await self._advance_upload(data, data["pack"], auth):
                    data["stage"], data["error"] = "completed", None
                self._save(data)
                if data["stage"] == "completed":
                    from .hub_catalog_cache import invalidate_hub_catalog

                    invalidate_hub_catalog("skill")
            except (PublishValidationError, PublishStoreError) as exc:
                data["stage"], data["error"] = "blocked", {"code": exc.code}
                self._save(data)
            except (HubProtocolError, OSError) as exc:
                # Network failures keep the same persisted operation. They never
                # select another profile or cause a second POST.
                if getattr(exc, "status_code", None) in {401, 403}:
                    data["stage"], data["error"] = "reauth_required", {"code": "AUTH_REQUIRED"}
                else:
                    data["error"] = {"code": "SKILLPACK_HUB_CHECK_FAILED"}
                self._save(data)
            except (ValueError, TypeError, KeyError):
                data["stage"], data["error"] = "blocked", {"code": "SKILLPACK_INVALID_STATE"}
                self._save(data)

    async def _advance_upload(self, data: dict, item: dict, auth: PublishAuth) -> bool:
        is_pack = item["kind"] == "skillpack"
        scope = data["scope"]
        if not item.get("operation_id"):
            data["stage"] = "publishing_pack" if is_pack else "publishing_members"
            self._save(data)
            operation = await self.service.commit(
                scope, item["draft_id"], self._request_id(data, item), auth=auth,
            )
            item["operation_id"] = operation["operation_id"]
            self._save(data)
        operation = self.service.status(scope, item["operation_id"])
        if operation["execution_status"] in {"queued", "uploading"}:
            return False
        result = operation.get("result") or {}
        if operation["execution_status"] == "unknown":
            result = await self.hub.recover_upload(
                item["name"], item["version"], item["artifact_sha256"], data["publisher_id"], auth,
            )
            if result is None:
                data["stage"], data["error"] = "reconcile_upload", {"code": "SKILLPACK_UPLOAD_UNCONFIRMED"}
                return False
        elif operation["execution_status"] == "failed":
            code = (operation.get("error") or {}).get("code", "publish_failed")
            if code not in {"interrupted", "artifact_unavailable", "rate_limited"} or item.get("attempt", 0) >= 3:
                raise PublishValidationError(code)
            recovered = await self.hub.recover_upload(
                item["name"], item["version"], item["artifact_sha256"], data["publisher_id"], auth,
            )
            if recovered:
                result = recovered
            else:
                await self._retry_known_failure(data, item)
                return False
        asset_id = result.get("asset_id")
        if not asset_id:
            raise PublishValidationError("SKILLPACK_UPLOAD_UNCONFIRMED")
        item["asset_id"] = asset_id
        self._save(data)
        review = await self.hub.review(asset_id, item["version"], auth)
        expected_type = self._type(data["transport_profile"]) if is_pack else "skill"
        if review.get("plugin_type") != expected_type or review.get("name") != item["name"]:
            raise PublishValidationError("SKILLPACK_MEMBER_IDENTITY_CONFLICT")
        state = review.get("publish_result")
        item["review"] = state
        if state == "publish_failed" or review.get("version_moderation_status") == "REJECTED":
            item["error"] = {"code": "SKILLPACK_REVIEW_REJECTED"}
            raise PublishValidationError("SKILLPACK_REVIEW_REJECTED")
        if state not in {"publish_success", "published"}:
            data["stage"] = "waiting_pack_review" if is_pack else "waiting_member_review"
            return False
        try:
            remote = await self.hub.public_artifact(asset_id, item["version"], expected_type)
        except HubNotFoundError:
            data["stage"] = "waiting_pack_review" if is_pack else "waiting_member_review"
            return False
        if remote["artifact_sha256"] != item["artifact_sha256"]:
            raise PublishValidationError("SKILLPACK_MEMBER_IDENTITY_CONFLICT")
        item["review"] = "approved"
        data["error"] = None
        self._save(data)
        return True

    @staticmethod
    def _request_id(data: dict, item: dict) -> str:
        return f"{data['id']}:{item['name']}:{item.get('attempt', 0)}"

    async def _retry_known_failure(self, data: dict, item: dict) -> None:
        """Rebuild the same confirmed bytes only after a proven failed attempt."""
        self._check_sources(data)
        previous = self.store.operation_request(data["scope"], item["operation_id"])
        checksum = item["artifact_sha256"]
        if item["kind"] == "skillpack":
            await self._prepare_pack(data)
        else:
            draft = await self.service.prepare(
                data["scope"], item["local_id"], previous.identity,
                {"visibility": "public"},
            )
            item["draft_id"] = draft["draft_id"]
            item["artifact_sha256"] = draft["checksum_sha256"]
        if item["artifact_sha256"] != checksum:
            self.service.discard_uncommitted_draft(data["scope"], item["draft_id"])
            raise PublishValidationError("SKILLPACK_CONTENT_CHANGED")
        item["attempt"] = item.get("attempt", 0) + 1
        item.pop("operation_id", None)
        data["stage"] = "publishing_pack" if item["kind"] == "skillpack" else "publishing_members"
        self._save(data)

    async def _prepare_pack(self, data: dict) -> None:
        pack = data["pack"]
        lock = SkillPackLock(pack["name"], pack["version"], tuple(
            SkillPackLockedMember(item["name"], item["asset_id"], item["version"], item["artifact_sha256"])
            for item in data["members"]
        ))
        # Only a private temporary root is materialized. Installed root and
        # members remain unchanged throughout publishing.
        with tempfile.TemporaryDirectory(prefix="skillpack-source-", dir=self.root) as directory:
            root = Path(directory)
            (root / "SKILL.md").write_text(pack["document"], encoding="utf-8")
            (root / "skillpack.lock.json").write_text(json.dumps(lock.to_dict()), encoding="utf-8")
            # The normal service resolver cannot accept browser-provided paths.
            # This server-created root is passed directly to its bounded builder.
            metadata = dict(data["metadata"])
            metadata["tags"] = list(dict.fromkeys([*metadata.get("tags", []), SKILLPACK_HUB_TAG]))
            metadata["transport_profile"] = data["transport_profile"]
            prepared = await self.service.prepare_snapshot(
                data["scope"], data["local_id"],
                PublishIdentity("skillpack", pack["name"], pack["version"], pack.get("target_asset_id")),
                metadata, root,
            )
        pack["draft_id"] = prepared["draft_id"]
        pack["artifact_sha256"] = prepared["checksum_sha256"]

    async def close(self) -> None:
        tasks = tuple(self._tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self._db.close()
