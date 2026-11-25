# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Persistent provenance for installed system plugin packages and bundles.

Both hosts read the same file; a file lock serializes read-modify-write so the
Gateway and AgentServer can update their own domain records without dropping
each other's changes. Bundle containers are written by the AgentServer only;
member records are written by the host that loaded the member.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
import tempfile
import time
from dataclasses import asdict, dataclass, replace
from datetime import datetime, timezone
from pathlib import Path

import portalocker

_LOCK_TIMEOUT = 5.0
_PACKAGE_STATUSES = {"pending", "active", "standby", "disabled", "failed", "uninstalling"}
_BUNDLE_STATUSES = {"pending", "active", "partial", "failed", "disabled", "uninstalling"}
_ORIGINS = {"hub", "user", "preset"}


@dataclass(frozen=True, slots=True)
class ExtensionInstallRecord:
    package_id: str
    version: str
    origin: str
    asset_id: str = ""
    install_path: str = ""
    active: bool = True
    enabled: bool = True
    installed_at: str = ""
    #: Host domain declared by the package manifest; empty for legacy records.
    domain: str = ""
    #: Load state for cross-host coordination: pending / active / standby / disabled / failed.
    status: str = "active"
    #: Compact failure summary for diagnostics and the detail page.
    error: str = ""
    #: Content fingerprint used to reject the same id+version with different bytes.
    digest: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "ExtensionInstallRecord | None":
        package_id = value.get("package_id")
        version = value.get("version")
        origin = value.get("origin")
        if not isinstance(package_id, str) or not package_id.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None
        if origin not in _ORIGINS:
            origin = "user"

        def text(key: str) -> str:
            raw = value.get(key)
            return raw.strip() if isinstance(raw, str) else ""

        active = value.get("active")
        enabled = value.get("enabled")
        status = value.get("status")
        return cls(
            package_id=package_id.strip(),
            version=version.strip(),
            origin=origin,
            asset_id=text("asset_id"),
            install_path=value.get("install_path") if isinstance(value.get("install_path"), str) else "",
            active=active if isinstance(active, bool) else True,
            enabled=enabled if isinstance(enabled, bool) else True,
            installed_at=text("installed_at"),
            domain=text("domain"),
            status=status if status in _PACKAGE_STATUSES else "active",
            error=text("error"),
            digest=text("digest"),
        )


@dataclass(frozen=True, slots=True)
class ExtensionBundleMember:
    """One member expected by an installed bundle container."""

    package_id: str
    version: str
    domain: str

    @classmethod
    def from_dict(cls, value: dict) -> "ExtensionBundleMember | None":
        package_id = value.get("package_id")
        version = value.get("version")
        domain = value.get("domain")
        if not isinstance(package_id, str) or not package_id.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None
        if not isinstance(domain, str) or not domain.strip():
            return None
        return cls(package_id=package_id.strip(), version=version.strip(), domain=domain.strip())


_MEMBER_STATUSES = frozenset({"pending", "active", "standby", "disabled", "failed"})


@dataclass(frozen=True, slots=True)
class BundleMemberRecord:
    """Runtime state of one bundle member instance.

    Bundle members are not standalone packages: they keep their own root
    inside the bundle copy and at most one instance per package id is active.
    """

    package_id: str
    version: str
    domain: str
    root: str = ""
    enabled: bool = False
    active: bool = False
    status: str = "disabled"
    digest: str = ""
    error: str = ""
    updated_at: str = ""

    @classmethod
    def from_dict(cls, value: dict) -> "BundleMemberRecord | None":
        package_id = value.get("package_id")
        version = value.get("version")
        domain = value.get("domain")
        if not isinstance(package_id, str) or not package_id.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None
        if not isinstance(domain, str) or not domain.strip():
            return None

        def text(key: str) -> str:
            raw = value.get(key)
            return raw.strip() if isinstance(raw, str) else ""

        enabled = value.get("enabled")
        active = value.get("active")
        status = value.get("status")
        return cls(
            package_id=package_id.strip(),
            version=version.strip(),
            domain=domain.strip(),
            root=text("root"),
            enabled=enabled if isinstance(enabled, bool) else False,
            active=active if isinstance(active, bool) else False,
            status=status if status in _MEMBER_STATUSES else "disabled",
            digest=text("digest"),
            error=text("error"),
            updated_at=text("updated_at"),
        )


@dataclass(frozen=True, slots=True)
class ActiveInstance:
    """The one instance currently allowed to load for a package id."""

    package_id: str
    version: str
    kind: str
    bundle_id: str = ""
    bundle_version: str = ""
    root: str = ""


@dataclass(frozen=True, slots=True)
class ExtensionBundleRecord:
    """One installed bundle container and the members it expects."""

    bundle_id: str
    version: str
    origin: str
    asset_id: str = ""
    members: tuple[ExtensionBundleMember, ...] = ()
    member_records: tuple[BundleMemberRecord, ...] = ()
    status: str = "pending"
    installed_at: str = ""
    error: str = ""
    #: True for the one installed version whose members are loaded.
    active: bool = False

    @classmethod
    def from_dict(cls, value: dict) -> "ExtensionBundleRecord | None":
        bundle_id = value.get("bundle_id")
        version = value.get("version")
        origin = value.get("origin")
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            return None
        if not isinstance(version, str) or not version.strip():
            return None
        if origin not in _ORIGINS:
            origin = "user"
        raw_members = value.get("members")
        parsed_members: list[ExtensionBundleMember] = []
        if isinstance(raw_members, list):
            for item in raw_members:
                if not isinstance(item, dict):
                    continue
                member = ExtensionBundleMember.from_dict(item)
                if member is not None:
                    parsed_members.append(member)
        status = value.get("status")
        asset_id = value.get("asset_id")
        installed_at = value.get("installed_at")
        error = value.get("error")
        active = value.get("active")
        raw_member_records = value.get("member_records")
        parsed_member_records: list[BundleMemberRecord] = []
        if isinstance(raw_member_records, list):
            for item in raw_member_records:
                if not isinstance(item, dict):
                    continue
                member_record = BundleMemberRecord.from_dict(item)
                if member_record is not None:
                    parsed_member_records.append(member_record)
        return cls(
            bundle_id=bundle_id.strip(),
            version=version.strip(),
            origin=origin,
            asset_id=asset_id.strip() if isinstance(asset_id, str) else "",
            members=tuple(parsed_members),
            member_records=tuple(parsed_member_records),
            status=status if status in _BUNDLE_STATUSES else "pending",
            installed_at=installed_at if isinstance(installed_at, str) else "",
            error=error.strip() if isinstance(error, str) else "",
            # A record written before multi-version bundles existed is active.
            active=active if isinstance(active, bool) else True,
        )


class ExtensionInstallState:
    """A small atomic JSON store keyed by ``package_id@version``.

    The file holds ``packages`` rows and an optional ``bundles`` array. A file
    written before bundles existed reads as an empty bundle list. Every
    mutation takes a cross-process file lock, so two hosts never overwrite each
    other's records.
    """

    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    @classmethod
    def for_install_dir(cls, install_dir: Path) -> "ExtensionInstallState":
        # 状态文件挂 extension_v2 数据根同名目录（与 install_dir 同根的 extension/ 下）；
        # 目录名经 get_extension_data_dir 派生，避免散落字面量
        from jiuwenswarm.common.paths import get_extension_data_dir

        return cls(
            Path(install_dir).expanduser().resolve().parent
            / get_extension_data_dir().name
            / "system_plugin_install_state.json"
        )

    # ── file access ───────────────────────────────────────────────────────

    def _load(self) -> tuple[dict[str, ExtensionInstallRecord], dict[str, ExtensionBundleRecord]]:
        source = self.path
        if not source.is_file():
            return {}, {}
        try:
            payload = json.loads(source.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}, {}
        if not isinstance(payload, dict):
            return {}, {}
        packages: dict[str, ExtensionInstallRecord] = {}
        raw_packages = payload.get("packages")
        if isinstance(raw_packages, list):
            for value in raw_packages:
                if not isinstance(value, dict):
                    continue
                record = ExtensionInstallRecord.from_dict(value)
                if record is not None:
                    packages[_key(record.package_id, record.version)] = record
        bundles: dict[str, ExtensionBundleRecord] = {}
        raw_bundles = payload.get("bundles")
        if isinstance(raw_bundles, list):
            for value in raw_bundles:
                if not isinstance(value, dict):
                    continue
                record = ExtensionBundleRecord.from_dict(value)
                if record is not None:
                    bundles[_key(record.bundle_id, record.version)] = record
        return packages, bundles

    def _write(
        self,
        records: dict[str, ExtensionInstallRecord],
        bundles: dict[str, ExtensionBundleRecord],
    ) -> None:
        payload: dict[str, list[dict]] = {"packages": [asdict(records[key]) for key in sorted(records)]}
        if bundles:
            payload["bundles"] = [asdict(bundles[key]) for key in sorted(bundles)]
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=self.path.parent,
                prefix=f".{self.path.name}.",
                suffix=".tmp",
                delete=False,
            ) as stream:
                temp_path = Path(stream.name)
                json.dump(payload, stream, ensure_ascii=False, indent=2)
                stream.write("\n")
            for attempt in range(5):
                try:
                    temp_path.replace(self.path)
                    break
                except OSError:
                    if attempt == 4:
                        raise
                    time.sleep(0.02 * (attempt + 1))
        finally:
            if temp_path is not None:
                temp_path.unlink(missing_ok=True)

    def _transaction(self, change) -> object:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        lock_path = self.path.with_suffix(self.path.suffix + ".lock")
        with portalocker.Lock(
            str(lock_path), mode="a+b", timeout=_LOCK_TIMEOUT, check_interval=0.05,
        ):
            packages, bundles = self._load()
            result, write = change(packages, bundles)
            if write:
                self._write(packages, bundles)
            return result

    # ── package records ───────────────────────────────────────────────────

    def get(self, package_id: str, version: str = "") -> ExtensionInstallRecord | None:
        records = [
            record
            for record in self._load()[0].values()
            if record.package_id == package_id and (not version or record.version == version)
        ]
        if not records:
            return None
        return max(records, key=_record_preference)

    def packages(self) -> list[ExtensionInstallRecord]:
        """Return every package record, including both host domains."""

        return sorted(
            self._load()[0].values(),
            key=lambda record: (record.package_id, _version_text_key(record.version)),
        )

    def by_asset(self, asset_id: str) -> ExtensionInstallRecord | None:
        if not asset_id:
            return None
        records = [record for record in self._load()[0].values() if record.asset_id == asset_id]
        return max(records, key=_record_preference) if records else None

    def upsert(self, record: ExtensionInstallRecord) -> None:
        def change(packages, bundles):
            key = _key(record.package_id, record.version)
            previous = packages.get(key)
            packages[key] = _with_defaults(record) if previous is None else _updated(previous, record)
            return None, True

        self._transaction(change)

    def upsert_many(self, records: Sequence[ExtensionInstallRecord]) -> None:
        """Write several package records in one locked transaction."""

        if not records:
            return

        def change(packages, bundles):
            for record in records:
                key = _key(record.package_id, record.version)
                previous = packages.get(key)
                packages[key] = _with_defaults(record) if previous is None else _updated(previous, record)
            return None, True

        self._transaction(change)

    def activate(self, record: ExtensionInstallRecord) -> None:
        """Record one version as the loaded copy and retire the package's other versions."""

        def change(packages, bundles):
            for key, existing in list(packages.items()):
                if existing.package_id != record.package_id or existing.version == record.version:
                    continue
                packages[key] = _retired(existing)
            key = _key(record.package_id, record.version)
            previous = packages.get(key)
            updated = _with_defaults(record)
            packages[key] = ExtensionInstallRecord(
                package_id=updated.package_id,
                version=updated.version,
                origin=updated.origin,
                asset_id=updated.asset_id,
                install_path=updated.install_path,
                active=True,
                enabled=True,
                installed_at=updated.installed_at
                or (previous.installed_at if previous else "")
                or _timestamp(),
                domain=updated.domain or (previous.domain if previous else ""),
                status="active",
                error="",
                digest=updated.digest or (previous.digest if previous else ""),
            )
            return None, True

        self._transaction(change)

    def mark(self, package_id: str, *, version: str = "", active: bool | None = None,
             enabled: bool | None = None, status: str = "", error: str = "") -> None:
        def change(packages, bundles):
            changed = False
            for key, record in list(packages.items()):
                if record.package_id != package_id or (version and record.version != version):
                    continue
                packages[key] = ExtensionInstallRecord(
                    package_id=record.package_id,
                    version=record.version,
                    origin=record.origin,
                    asset_id=record.asset_id,
                    install_path=record.install_path,
                    active=record.active if active is None else active,
                    enabled=record.enabled if enabled is None else enabled,
                    installed_at=record.installed_at,
                    domain=record.domain,
                    status=status if status in _PACKAGE_STATUSES else record.status,
                    error=error,
                    digest=record.digest,
                )
                changed = True
            return None, changed

        self._transaction(change)

    def remove(self, package_id: str, version: str = "") -> None:
        def change(packages, bundles):
            kept = {
                key: record
                for key, record in packages.items()
                if record.package_id != package_id or (version and record.version != version)
            }
            if len(kept) == len(packages):
                return None, False
            packages.clear()
            packages.update(kept)
            return None, True

        self._transaction(change)

    # ── bundle container records ──────────────────────────────────────────

    def bundles(self) -> list[ExtensionBundleRecord]:
        """Return every bundle record, newest version first per bundle."""

        records = list(self._load()[1].values())
        return sorted(
            records,
            key=lambda record: (record.bundle_id, _version_text_key(record.version)),
            reverse=True,
        )

    def get_bundle(self, bundle_id: str, version: str = "") -> ExtensionBundleRecord | None:
        records = [
            record
            for record in self._load()[1].values()
            if record.bundle_id == bundle_id and (not version or record.version == version)
        ]
        if not records:
            return None
        return max(records, key=lambda record: _version_text_key(record.version))

    def bundle_of(self, package_id: str) -> ExtensionBundleRecord | None:
        """Return the bundle that claims ``package_id`` as a member, if any."""

        if not package_id:
            return None
        for record in self.bundles():
            if any(member.package_id == package_id for member in record.members):
                return record
        return None

    def bundle_members(
        self, bundle_id: str = "", version: str = "",
    ) -> list[BundleMemberRecord]:
        """Return member instances, optionally only one bundle version."""

        result: list[BundleMemberRecord] = []
        for record in self.bundles():
            if bundle_id and record.bundle_id != bundle_id:
                continue
            if version and record.version != version:
                continue
            result.extend(record.member_records)
        return result

    def get_bundle_member(
        self, bundle_id: str, bundle_version: str, package_id: str,
    ) -> BundleMemberRecord | None:
        record = self.get_bundle(bundle_id, bundle_version)
        if record is None:
            return None
        for member in record.member_records:
            if member.package_id == package_id:
                return member
        return None

    def upsert_bundle_member(
        self, bundle_id: str, bundle_version: str, member: BundleMemberRecord,
    ) -> None:
        """Insert or replace one member instance of a bundle version."""

        def change(packages, bundles):
            key = _key(bundle_id, bundle_version)
            record = bundles.get(key)
            if record is None:
                return None, False
            kept = tuple(item for item in record.member_records if item.package_id != member.package_id)
            bundles[key] = replace(record, member_records=(*kept, member))
            return None, True

        self._transaction(change)

    def upsert_bundle_members(
        self, bundle_id: str, bundle_version: str, records: Sequence[BundleMemberRecord],
    ) -> None:
        """Insert or replace several member instances in one transaction."""

        if not records:
            return

        def change(packages, bundles):
            key = _key(bundle_id, bundle_version)
            record = bundles.get(key)
            if record is None:
                return None, False
            by_id = {item.package_id: item for item in record.member_records}
            for item in records:
                by_id[item.package_id] = item
            bundles[key] = replace(record, member_records=tuple(by_id.values()))
            return None, True

        self._transaction(change)

    def mark_bundle_member(
        self,
        bundle_id: str,
        bundle_version: str,
        package_id: str,
        *,
        enabled: bool | None = None,
        active: bool | None = None,
        status: str = "",
        root: str = "",
        error: str = "",
    ) -> None:
        """Update the runtime state of one bundle member instance."""

        def change(packages, bundles):
            key = _key(bundle_id, bundle_version)
            record = bundles.get(key)
            if record is None:
                return None, False
            changed = False
            updated: list[BundleMemberRecord] = []
            for member in record.member_records:
                if member.package_id != package_id:
                    updated.append(member)
                    continue
                status_value = status if status in _MEMBER_STATUSES else member.status
                replacement = replace(
                    member,
                    enabled=member.enabled if enabled is None else enabled,
                    active=member.active if active is None else active,
                    status=status_value,
                    root=root or member.root,
                    error=error,
                    updated_at=_timestamp(),
                )
                updated.append(replacement)
                changed = True
            if not changed:
                return None, False
            bundles[key] = replace(record, member_records=tuple(updated))
            return None, True

        self._transaction(change)

    def remove_bundle_member(
        self, bundle_id: str, bundle_version: str, package_id: str,
    ) -> None:
        def change(packages, bundles):
            key = _key(bundle_id, bundle_version)
            record = bundles.get(key)
            if record is None:
                return None, False
            kept = tuple(item for item in record.member_records if item.package_id != package_id)
            if len(kept) == len(record.member_records):
                return None, False
            bundles[key] = replace(record, member_records=kept)
            return None, True

        self._transaction(change)

    def remove_bundle_members(self, bundle_id: str, version: str = "") -> None:
        def change(packages, bundles):
            changed = False
            for key, record in list(bundles.items()):
                if record.bundle_id != bundle_id or (version and record.version != version):
                    continue
                if record.member_records:
                    bundles[key] = replace(record, member_records=())
                    changed = True
            return None, changed

        self._transaction(change)

    def active_instance(self, package_id: str) -> ActiveInstance | None:
        """Return the one instance marked active for a package id, if any."""

        candidates: list[tuple[tuple[bool, bool, str], ActiveInstance]] = []
        install_dir = self.path.resolve().parents[1]
        for record in self.packages():
            if record.package_id != package_id or not record.active:
                continue
            candidates.append(
                (
                    (record.active, record.enabled, record.installed_at),
                    ActiveInstance(
                        package_id=package_id,
                        version=record.version,
                        kind="standalone",
                        root=record.install_path or str(install_dir / package_id / record.version),
                    ),
                )
            )
        for bundle in self.bundles():
            for member in bundle.member_records:
                if member.package_id != package_id or not member.active:
                    continue
                candidates.append(
                    (
                        (member.active, member.enabled, member.updated_at),
                        ActiveInstance(
                            package_id=package_id,
                            version=member.version,
                            kind="bundle",
                            bundle_id=bundle.bundle_id,
                            bundle_version=bundle.version,
                            root=member.root or str(
                                install_dir / bundle.bundle_id / bundle.version
                                / "plugins" / package_id
                            ),
                        ),
                    )
                )
        if not candidates:
            return None
        return max(candidates, key=lambda item: item[0])[1]

    def reconcile_claims(self) -> None:
        """Demote duplicate active instances left by a crash or manual edit."""

        package_ids = {record.package_id for record in self.packages() if record.active}
        package_ids.update(member.package_id for member in self.bundle_members() if member.active)
        for package_id in sorted(package_ids):
            claim = self.active_instance(package_id)
            if claim is not None:
                self.switch_active_instance(claim)

    def switch_active_instance(self, claim: ActiveInstance) -> None:
        """Make one instance active and demote every other instance of the id."""

        def change(packages, bundles):
            for key, record in list(packages.items()):
                if record.package_id != claim.package_id:
                    continue
                target = claim.kind == "standalone" and record.version == claim.version
                packages[key] = replace(
                    record,
                    active=True if target else False,
                    enabled=True if target else record.enabled,
                    status="active" if target else (
                        "failed" if record.status == "failed"
                        else "standby" if record.enabled else "disabled"
                    ),
                    error="" if target else record.error,
                )
            for key, bundle in list(bundles.items()):
                updated: list[BundleMemberRecord] = []
                changed = False
                for member in bundle.member_records:
                    if member.package_id != claim.package_id:
                        updated.append(member)
                        continue
                    target = (
                        claim.kind == "bundle"
                        and bundle.bundle_id == claim.bundle_id
                        and bundle.version == claim.bundle_version
                        and member.version == claim.version
                    )
                    updated.append(
                        replace(
                            member,
                            active=True if target else False,
                            enabled=True if target else member.enabled,
                            status="active" if target else (
                                "failed" if member.status == "failed"
                                else "standby" if member.enabled else "disabled"
                            ),
                            error="" if target else member.error,
                            updated_at=_timestamp(),
                        )
                    )
                    changed = True
                if changed:
                    bundles[key] = replace(bundle, member_records=tuple(updated))
            return None, True

        self._transaction(change)

    def disable_instance(
        self,
        package_id: str,
        *,
        kind: str,
        version: str = "",
        bundle_id: str = "",
        bundle_version: str = "",
    ) -> None:
        """Disable one instance without promoting any other instance."""

        def change(packages, bundles):
            changed = False
            for key, record in list(packages.items()):
                if record.package_id != package_id:
                    continue
                if kind != "standalone" or (version and record.version != version):
                    continue
                packages[key] = replace(
                    record, active=False, enabled=False, status="disabled",
                )
                changed = True
            for key, bundle in list(bundles.items()):
                if kind == "bundle":
                    wrong_bundle = bundle.bundle_id != bundle_id
                    wrong_version = bool(bundle_version) and bundle.version != bundle_version
                    if wrong_bundle or wrong_version:
                        continue
                updated: list[BundleMemberRecord] = []
                local = False
                for member in bundle.member_records:
                    if member.package_id != package_id:
                        updated.append(member)
                        continue
                    if kind == "bundle" and version and member.version != version:
                        updated.append(member)
                        continue
                    if kind == "standalone":
                        updated.append(member)
                        continue
                    updated.append(
                        replace(
                            member, active=False, enabled=False, status="disabled",
                            updated_at=_timestamp(),
                        )
                    )
                    local = True
                if local:
                    bundles[key] = replace(bundle, member_records=tuple(updated))
                    changed = True
            return None, changed

        self._transaction(change)

    def upsert_bundle(self, record: ExtensionBundleRecord) -> None:
        def change(packages, bundles):
            key = _key(record.bundle_id, record.version)
            previous = bundles.get(key)
            bundles[key] = ExtensionBundleRecord(
                bundle_id=record.bundle_id,
                version=record.version,
                origin=record.origin or (previous.origin if previous else "user"),
                asset_id=record.asset_id or (previous.asset_id if previous else ""),
                members=record.members or (previous.members if previous else ()),
                member_records=record.member_records or (previous.member_records if previous else ()),
                status=record.status if record.status in _BUNDLE_STATUSES else (
                    previous.status if previous else "pending"
                ),
                installed_at=record.installed_at or (previous.installed_at if previous else "") or _timestamp(),
                error=record.error,
                active=record.active or (previous.active if previous else False),
            )
            return None, True

        self._transaction(change)

    def active_bundle(self, bundle_id: str) -> ExtensionBundleRecord | None:
        """Return the active version, falling back to the newest installed one."""

        newest: ExtensionBundleRecord | None = None
        for record in self.bundles():
            if record.bundle_id != bundle_id:
                continue
            if record.active:
                return record
            if newest is None:
                newest = record
        return newest

    def activate_bundle(self, bundle_id: str, version: str) -> ExtensionBundleRecord | None:
        """Mark one version active and every other version of the same bundle inactive."""

        def change(packages, bundles):
            key = _key(bundle_id, version)
            if key not in bundles:
                return None, False
            changed = False
            for other_key, other in list(bundles.items()):
                if other.bundle_id != bundle_id:
                    continue
                active = other_key == key
                if other.active != active:
                    bundles[other_key] = replace(other, active=active)
                    changed = True
            return None, changed

        self._transaction(change)
        return self.get_bundle(bundle_id, version)

    def deactivate_bundle(self, bundle_id: str, version: str = "") -> None:
        """Clear the active flag for one version, or every version of the bundle."""

        def change(packages, bundles):
            changed = False
            for key, record in list(bundles.items()):
                if record.bundle_id != bundle_id or (version and record.version != version):
                    continue
                if record.active:
                    bundles[key] = replace(record, active=False)
                    changed = True
            return None, changed

        self._transaction(change)

    def remove_bundle(self, bundle_id: str, version: str = "") -> None:
        def change(packages, bundles):
            kept = {
                key: record
                for key, record in bundles.items()
                if record.bundle_id != bundle_id or (version and record.version != version)
            }
            if len(kept) == len(bundles):
                return None, False
            bundles.clear()
            bundles.update(kept)
            return None, True

        self._transaction(change)


def _with_defaults(record: ExtensionInstallRecord) -> ExtensionInstallRecord:
    return ExtensionInstallRecord(
        package_id=record.package_id,
        version=record.version,
        origin=record.origin,
        asset_id=record.asset_id,
        install_path=record.install_path,
        active=record.active,
        enabled=record.enabled,
        installed_at=record.installed_at or _timestamp(),
        domain=record.domain,
        status=record.status if record.status in _PACKAGE_STATUSES else "active",
        error=record.error,
        digest=record.digest,
    )


def _retired(record: ExtensionInstallRecord) -> ExtensionInstallRecord:
    return ExtensionInstallRecord(
        package_id=record.package_id,
        version=record.version,
        origin=record.origin,
        asset_id=record.asset_id,
        install_path=record.install_path,
        active=False,
        enabled=False,
        installed_at=record.installed_at,
        domain=record.domain,
        status=record.status,
        error=record.error,
        digest=record.digest,
    )


def _updated(previous: ExtensionInstallRecord, record: ExtensionInstallRecord) -> ExtensionInstallRecord:
    return ExtensionInstallRecord(
        package_id=record.package_id,
        version=record.version,
        origin=record.origin or previous.origin,
        asset_id=record.asset_id or previous.asset_id,
        install_path=record.install_path or previous.install_path,
        active=record.active,
        enabled=record.enabled,
        installed_at=record.installed_at or previous.installed_at or _timestamp(),
        domain=record.domain or previous.domain,
        status=record.status if record.status in _PACKAGE_STATUSES else previous.status,
        error=record.error,
        digest=record.digest or previous.digest,
    )


def _key(package_id: str, version: str) -> str:
    return f"{package_id}@{version}"


def _timestamp() -> str:
    return datetime.now(timezone.utc).isoformat()


def _record_preference(record: ExtensionInstallRecord) -> tuple[bool, bool, str]:
    """Prefer the loaded copy, then the enabled copy, then the newest record."""

    return record.active, record.enabled, record.installed_at


def _version_text_key(version: str) -> tuple[int, int, int, str]:
    parts = str(version).split(".")
    numbers: list[int] = []
    for part in parts[:3]:
        digits = "".join(ch for ch in part if ch.isdigit())
        numbers.append(int(digits) if digits else 0)
    while len(numbers) < 3:
        numbers.append(0)
    return numbers[0], numbers[1], numbers[2], str(version)
