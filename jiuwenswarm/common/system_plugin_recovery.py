# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Durable, process-independent recovery history for system plugin packages.

The ledger only reads and writes managed ``manifest.json`` files.  It never
imports a package module, so a separate recovery process can still operate
when an enabled package blocks an AgentServer or Gateway event loop.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import Message
from pathlib import Path
from typing import Any, Iterator, Mapping, Sequence

import portalocker

_HISTORY_FILE = "system_plugin_recovery.json"
_ENDPOINT_FILE = "system_plugin_recovery_endpoint.json"
_RESTART_FILE = "system_plugin_recovery_restart.json"
_LOCK_FILE = "system_plugin_recovery.lock"
_MAX_RECORDS = 50
_SCHEMA_VERSION = 1
_INSTALL_STATE_FILE = "system_plugin_install_state.json"
_STATUSES = frozenset({"pending", "active", "standby", "disabled", "failed", "uninstalling"})
_RESTART_DELAY_SECONDS = 1.0
_INSTALL_DIR_ENV = "EXTENSION_INSTALL_DIR"
_DATA_DIR_ENV = "EXTENSION_DATA_DIR"
_RESTART_SIGNAL_PATH_ENV = "JIUWENSWARM_RECOVERY_RESTART_SIGNAL_PATH"
RECOVERY_MAX_BODY_BYTES = 16 * 1024


def is_local_recovery_request(headers: Message, server_port: int) -> bool:
    """Require the recovery server's loopback authority and same-origin browser requests."""
    hosts = headers.get_all("Host", [])
    if len(hosts) != 1:
        return False
    authority = hosts[0].strip().lower()
    allowed = {f"127.0.0.1:{server_port}", f"localhost:{server_port}", f"[::1]:{server_port}"}
    if server_port == 80:
        allowed.update({"127.0.0.1", "localhost", "[::1]"})
    if authority not in allowed:
        return False
    origins = headers.get_all("Origin", [])
    expected_origin = f"http://{authority}"
    if server_port == 80:
        expected_origin = expected_origin.removesuffix(":80")
    return not origins or (len(origins) == 1 and origins[0].lower() == expected_origin)


class RecoveryRecordNotFound(LookupError):
    """Raised when a requested history record is no longer retained."""


class SystemPluginRecovery:
    """Record package states and restore a retained state without loading code."""

    def __init__(self, install_dir: Path | None = None, data_dir: Path | None = None) -> None:
        from jiuwenswarm.common.paths import get_extension_data_dir, get_extension_pkgs_dir

        resolved_install_dir = install_dir or _resolve_directory(
            "install_dir",
            _INSTALL_DIR_ENV,
            get_extension_pkgs_dir(),
        )
        resolved_data_dir = data_dir or _resolve_directory(
            "data_dir",
            _DATA_DIR_ENV,
            get_extension_data_dir(),
        )
        self.install_dir = resolved_install_dir
        self.data_dir = resolved_data_dir
        self.install_dir = self.install_dir.expanduser().resolve()
        self.data_dir = self.data_dir.expanduser().resolve()
        self.path = self.data_dir / _HISTORY_FILE
        self.endpoint_path = self.data_dir / _ENDPOINT_FILE
        self.restart_path = self.data_dir / _RESTART_FILE
        self.lock_path = self.data_dir / _LOCK_FILE

    def begin_session(self, session_id: str) -> dict[str, Any]:
        """Append one startup snapshot for ``session_id`` without duplicating it."""
        if not _valid_session_id(session_id):
            raise ValueError("recovery session id is invalid")
        with self._locked():
            document = self._read_history()
            if document.get("session_id") == session_id:
                records = document.get("records")
                if isinstance(records, list) and records:
                    return _public_record(records[-1])
            record = self._record("startup", ())
            if document.get("schema_version") == _SCHEMA_VERSION and isinstance(
                document.get("records"), list
            ):
                records = _records(document)
                records.append(record)
                document["session_id"] = session_id
                document["records"] = records[-_MAX_RECORDS:]
            else:
                document = {
                    "schema_version": _SCHEMA_VERSION,
                    "session_id": session_id,
                    "records": [record],
                }
            self._write_history(document)
            self.restart_path.unlink(missing_ok=True)
            return _public_record(record)

    def record(self, action: str, package_ids: tuple[str, ...] | list[str]) -> dict[str, Any]:
        """Append a successful management operation and its resulting package state."""
        if action not in {"install", "uninstall", "enable", "disable"}:
            raise ValueError("recovery action is invalid")
        with self._locked():
            document = self._ensure_history()
            record = self._record(action, package_ids)
            records = _records(document)
            records.append(record)
            document["records"] = records[-_MAX_RECORDS:]
            if action == "install":
                # The launcher records "startup" before the initial plugins install.
                self._settle_startup_locked(document)
            self._write_history(document)
            return _public_record(record)

    def settle_startup(self) -> None:
        """Refresh the current startup snapshot after extensions finished loading."""
        with self._locked():
            document = self._read_history()
            if self._settle_startup_locked(document):
                self._write_history(document)

    def _settle_startup_locked(self, document: dict[str, Any]) -> bool:
        """Rewrite a startup record that predates the initial plugin installation.

        The launcher snapshots the install directory before child services install
        the preset plugins, so a first-run "startup" point can be empty. Refresh it
        while the session only gained installs and no package existed before it;
        once the set is stable, refresh it after startup loading so statuses match
        what actually loaded. The first enable/disable/uninstall freezes it.
        """
        records = _records(document)
        index = max((i for i, record in enumerate(records) if record.get("action") == "startup"), default=-1)
        if index < 0:
            return False
        if any(record.get("action") not in {"install"} for record in records[index + 1:]):
            return False
        previous = records[index].get("snapshot")
        snapshot = self._scan_packages()
        if isinstance(previous, list) and previous:
            previous_ids = {
                (item.get("package_id"), item.get("version"))
                for item in previous if isinstance(item, Mapping)
            }
            snapshot_ids = {(item["package_id"], item["version"]) for item in snapshot}
            if previous_ids != snapshot_ids:
                # First-run installs may extend the initial set; a package that
                # existed before the record makes it a user baseline instead.
                installed_after: set[str] = set()
                for record in records[index + 1:]:
                    if record.get("action") != "install":
                        continue
                    for package_id in record.get("package_ids") or ():
                        if isinstance(package_id, str):
                            installed_after.add(package_id)
                if any(
                    isinstance(item, Mapping) and item.get("package_id") not in installed_after
                    for item in previous
                ):
                    return False
        if previous == snapshot:
            return False
        records[index] = {**records[index], "snapshot": snapshot}
        document["records"] = records[-_MAX_RECORDS:]
        return True

    def consume_backend_restart(self, session_id: str) -> bool:
        """Consume the current session's delayed backend restart request."""
        if not _valid_session_id(session_id):
            return False
        with self._locked():
            try:
                request = json.loads(self.restart_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                return False
            if not isinstance(request, Mapping) or request.get("session_id") != session_id:
                self.restart_path.unlink(missing_ok=True)
                return False
            not_before = request.get("not_before")
            if not isinstance(not_before, (int, float)) or time.time() < not_before:
                return False
            self.restart_path.unlink(missing_ok=True)
            return True

    def records(self) -> list[dict[str, Any]]:
        """Return newest-first records suitable for a management page."""
        with self._locked():
            records = _records(self._read_history())
            return [_public_record(record) for record in reversed(records)]

    def preview(self, record_id: str) -> list[dict[str, Any]]:
        """Return the installed package states that restoring a record would produce.

        The preview applies the same matching rule as :meth:`restore` without
        modifying a manifest or appending a recovery record. Packages removed
        from the managed install directory cannot be restored and are omitted;
        packages installed after the selected record remain installed but are
        shown as disabled.
        """
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("recovery record id is invalid")
        with self._locked():
            source = next(
                (record for record in _records(self._read_history()) if record.get("id") == record_id),
                None,
            )
            if source is None:
                raise RecoveryRecordNotFound(record_id)
            expected = _expected_states(source)
            return [
                {
                    "package_id": entry["package_id"],
                    "version": entry["version"],
                    "enabled": _target_state(entry, expected)["enabled"],
                    **({"bundle_id": entry["bundle_id"], "bundle_version": entry["bundle_version"]}
                       if entry.get("bundle_id") else {}),
                }
                for entry in self._scan_entries()
            ]

    def restore(self, record_id: str) -> dict[str, Any]:
        """Persist the requested enabled states and append the restore result."""
        if not isinstance(record_id, str) or not record_id:
            raise ValueError("recovery record id is invalid")
        with self._locked():
            document = self._ensure_history()
            source = next((record for record in _records(document) if record.get("id") == record_id), None)
            if source is None:
                raise RecoveryRecordNotFound(record_id)
            expected = _expected_states(source)
            changed = self._apply_snapshot(expected)
            record = self._record("restore", changed, source_record_id=record_id)
            records = _records(document)
            records.append(record)
            document["records"] = records[-_MAX_RECORDS:]
            self._write_history(document)
            self._request_backend_restart(document, record_id)
            return _public_record(record)

    def endpoint(self) -> str | None:
        """Return the loopback recovery page URL published by the sidecar."""
        try:
            document = json.loads(self.endpoint_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return None
        value = document.get("url") if isinstance(document, Mapping) else None
        return value if isinstance(value, str) and value.startswith("http://127.0.0.1:") else None

    def publish_endpoint(self, url: str, session_id: str) -> None:
        """Atomically publish the current sidecar URL for the regular settings page."""
        if not url.startswith("http://127.0.0.1:") or not _valid_session_id(session_id):
            raise ValueError("recovery endpoint is invalid")
        self.data_dir.mkdir(parents=True, exist_ok=True)
        _write_json(self.endpoint_path, {"url": url, "session_id": session_id})

    @contextmanager
    def _locked(self) -> Iterator[None]:
        self.data_dir.mkdir(parents=True, exist_ok=True)
        with portalocker.Lock(str(self.lock_path), mode="a+", timeout=5):
            yield

    def _ensure_history(self) -> dict[str, Any]:
        document = self._read_history()
        if document.get("schema_version") == _SCHEMA_VERSION and isinstance(document.get("records"), list):
            return document
        return {
            "schema_version": _SCHEMA_VERSION,
            "session_id": os.getenv("JIUWENSWARM_RECOVERY_SESSION_ID", uuid.uuid4().hex),
            "records": [self._record("startup", ())],
        }

    def _read_history(self) -> dict[str, Any]:
        try:
            document = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return document if isinstance(document, dict) else {}

    def _write_history(self, document: dict[str, Any]) -> None:
        _write_json(self.path, document)

    def _request_backend_restart(self, document: Mapping[str, Any], record_id: str) -> None:
        session_id = document.get("session_id")
        if not _valid_session_id(session_id):
            return
        request = {
            "session_id": session_id,
            "record_id": record_id,
            # Let the management endpoint return before the parent takes down
            # its AgentServer and Gateway children.
            "not_before": time.time() + _RESTART_DELAY_SECONDS,
        }
        _write_json(self.restart_path, request)
        signal_path = os.getenv(_RESTART_SIGNAL_PATH_ENV, "").strip()
        if signal_path:
            try:
                _write_json(Path(signal_path), request)
            except OSError:
                # The Electron notification is an optional bridge. The durable
                # request in ``restart_path`` remains available to Python
                # launchers when that bridge cannot be written.
                pass

    def _record(
        self,
        action: str,
        package_ids: tuple[str, ...] | list[str],
        *,
        source_record_id: str | None = None,
    ) -> dict[str, Any]:
        record = {
            "id": uuid.uuid4().hex,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "package_ids": sorted(
                {
                    package_id
                    for package_id in package_ids
                    if isinstance(package_id, str)
                }
            ),
            "snapshot": self._scan_packages(),
        }
        if source_record_id is not None:
            record["source_record_id"] = source_record_id
        return record

    def _scan_packages(self) -> list[dict[str, Any]]:
        """Return the loadable copies a recovery snapshot needs to remember."""
        return [_public_package_state(entry) for entry in self._scan_entries()]

    def _scan_entries(self) -> list[dict[str, Any]]:
        """Scan standalone copies and bundle members, enriched with ledger state."""
        entries: list[dict[str, Any]] = []
        if not self.install_dir.is_dir():
            return entries
        state = self._install_state_indexes()
        for package_dir in sorted(self.install_dir.iterdir(), key=lambda path: path.name):
            if package_dir.is_symlink() or not package_dir.is_dir() or package_dir.name.startswith("."):
                continue
            for version_dir in sorted(package_dir.iterdir(), key=lambda path: path.name):
                if version_dir.is_symlink() or not version_dir.is_dir() or version_dir.name.startswith("."):
                    continue
                document = _read_manifest(version_dir)
                if document is None:
                    continue
                if document.get("is_bundle") is True:
                    entries.extend(self._bundle_entries(package_dir.name, version_dir, document, state))
                    continue
                entry = _standalone_entry(self.install_dir, package_dir.name, version_dir, document, state)
                if entry is not None:
                    entries.append(entry)
        return entries

    def _bundle_entries(
        self,
        bundle_id: str,
        version_dir: Path,
        document: Mapping[str, Any],
        state: Mapping[str, Any],
    ) -> list[dict[str, Any]]:
        """Return one entry per member copy of a bundle version."""
        package = document.get("package")
        if not isinstance(package, Mapping) or package.get("id") != bundle_id:
            return []
        if package.get("version") != version_dir.name:
            return []
        declared = document.get("members")
        paths = [
            item["path"]
            for item in (declared if isinstance(declared, list) else ())
            if isinstance(item, Mapping) and isinstance(item.get("path"), str) and item["path"]
        ]
        if not paths:
            members_dir = version_dir / "plugins"
            if members_dir.is_symlink() or not members_dir.is_dir():
                return []
            paths = [
                f"plugins/{path.name}"
                for path in sorted(members_dir.iterdir(), key=lambda item: item.name)
                if path.is_dir() and not path.name.startswith(".")
            ]
        entries: list[dict[str, Any]] = []
        for relative in paths:
            candidate = version_dir / relative
            if candidate.is_symlink():
                continue
            member_dir = candidate.resolve()
            try:
                if not member_dir.is_relative_to(version_dir.resolve()):
                    continue
            except OSError:
                continue
            if not member_dir.is_dir() or member_dir.name.startswith("."):
                continue
            member_document = _read_manifest(member_dir)
            if member_document is None or member_document.get("is_bundle") is True:
                continue
            entry = _member_entry(
                self.install_dir, bundle_id, version_dir.name, version_dir,
                member_dir, member_document, state,
            )
            if entry is not None:
                entries.append(entry)
        return entries

    def _apply_snapshot(self, expected: Mapping[tuple[str, ...], Mapping[str, Any]]) -> list[str]:
        """Write manifests and the shared ledger, rolling back on a write failure."""
        entries = self._scan_entries()
        pending: list[tuple[str, Path, dict[str, Any], dict[str, Any]]] = []
        changed: set[str] = set()
        for entry in entries:
            if entry.get("status") == "uninstalling":
                # The uninstall finalizers still owe this copy; a restore must
                # not change it or its ledger row.
                continue
            target = _target_state(entry, expected)
            manifest_path = Path(entry["manifest_path"])
            try:
                document = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as error:
                raise ValueError("managed manifest is invalid") from error
            if not isinstance(document, dict):
                raise ValueError("managed manifest is invalid")
            current = document.get("enable") if isinstance(document.get("enable"), bool) else True
            if current == target["enabled"]:
                continue
            updated = dict(document)
            updated["enable"] = target["enabled"]
            pending.append((entry["package_id"], manifest_path, document, updated))
            changed.add(entry["package_id"])


        written: list[tuple[Path, dict[str, Any]]] = []
        try:
            for _, manifest_path, original, target in pending:
                _write_json(manifest_path, target)
                written.append((manifest_path, original))
            changed |= self._sync_install_state(entries, expected)
        except (OSError, TypeError, ValueError, portalocker.LockException):
            for manifest_path, original in reversed(written):
                try:
                    _write_json(manifest_path, original)
                except (OSError, TypeError, ValueError):
                    pass
            raise
        return sorted(changed)

    def _install_state_indexes(self) -> dict[str, Any]:
        """Return the shared ledger indexed by standalone, bundle and member keys."""
        document = self._read_install_state()
        packages: dict[tuple[str, str], dict[str, Any]] = {}
        bundles: dict[tuple[str, str], dict[str, Any]] = {}
        members: dict[tuple[str, str, str], dict[str, Any]] = {}
        for record in document.get("packages") or ():
            if isinstance(record, Mapping) and isinstance(record.get("package_id"), str):
                packages[(record["package_id"], str(record.get("version") or ""))] = dict(record)
        for bundle in document.get("bundles") or ():
            if not isinstance(bundle, Mapping) or not isinstance(bundle.get("bundle_id"), str):
                continue
            key = (bundle["bundle_id"], str(bundle.get("version") or ""))
            bundles[key] = dict(bundle)
            for member in bundle.get("member_records") or ():
                if isinstance(member, Mapping) and isinstance(member.get("package_id"), str):
                    members[(key[0], key[1], member["package_id"])] = dict(member)
        return {"packages": packages, "bundles": bundles, "members": members}

    def _read_install_state(self) -> dict[str, Any]:
        """Read the shared install ledger without importing the extension runtime."""
        try:
            document = json.loads(
                (self.data_dir / _INSTALL_STATE_FILE).read_text(encoding="utf-8"),
            )
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return document if isinstance(document, dict) else {}

    def _sync_install_state(
        self,
        entries: Sequence[Mapping[str, Any]],
        expected: Mapping[tuple[str, ...], Mapping[str, Any]],
    ) -> set[str]:
        """Apply the same enabled/active/status truth to the shared install ledger."""
        path = self.data_dir / _INSTALL_STATE_FILE
        if not path.is_file():
            return set()
        with portalocker.Lock(
            str(path) + ".lock", mode="a+b", timeout=5, check_interval=0.05,
        ):
            document = self._read_install_state()
            packages = document.get("packages")
            packages = packages if isinstance(packages, list) else []
            bundles = document.get("bundles")
            bundles = bundles if isinstance(bundles, list) else []
            package_index = {
                (record.get("package_id"), record.get("version")): record
                for record in packages if isinstance(record, dict)
            }
            bundle_index = {
                (record.get("bundle_id"), record.get("version")): record
                for record in bundles if isinstance(record, dict)
            }
            member_index: dict[tuple[Any, Any, Any], dict[str, Any]] = {}
            for bundle in bundles:
                if not isinstance(bundle, dict):
                    continue
                for member in bundle.get("member_records") or ():
                    if isinstance(member, dict) and isinstance(member.get("package_id"), str):
                        member_index[
                            (bundle.get("bundle_id"), bundle.get("version"), member["package_id"])
                        ] = member
            changed = False
            changed_ids: set[str] = set()
            touched: set[tuple[str, str]] = set()
            for entry in entries:
                target = _target_state(entry, expected)
                bundle_id = entry.get("bundle_id")
                if bundle_id:
                    bundle = bundle_index.get((bundle_id, entry.get("bundle_version")))
                    if bundle is not None and bundle.get("status") == "uninstalling":
                        continue
                    record = member_index.get(
                        (bundle_id, entry.get("bundle_version"), entry["package_id"]),
                    )
                    if record is None:
                        continue
                    if _apply_state_record(record, target):
                        changed = True
                        changed_ids.add(entry["package_id"])
                    touched.add((bundle_id, str(entry.get("bundle_version") or "")))
                else:
                    record = package_index.get((entry["package_id"], entry["version"]))
                    if record is None:
                        continue
                    if _apply_state_record(record, target):
                        changed = True
                        changed_ids.add(entry["package_id"])
            for key in sorted(touched):
                bundle = bundle_index.get(key)
                if bundle is not None and bundle.get("status") != "uninstalling":
                    changed = _derive_bundle_record(bundle) or changed
            if changed:
                _write_json(path, document)
            return changed_ids


def begin_system_plugin_recovery_session(
    install_dir: Path | None = None,
    data_dir: Path | None = None,
) -> str:
    """Start the current process session before any extension code is loaded."""
    session_id = os.getenv("JIUWENSWARM_RECOVERY_SESSION_ID", "").strip()
    if not session_id:
        session_id = uuid.uuid4().hex
    SystemPluginRecovery(install_dir, data_dir).begin_session(session_id)
    return session_id


def _records(document: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = document.get("records")
    return [record for record in raw if isinstance(record, dict)] if isinstance(raw, list) else []


def _snapshot(record: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw = record.get("snapshot")
    if not isinstance(raw, list):
        raise ValueError("recovery record snapshot is invalid")
    result: list[dict[str, Any]] = []
    for item in raw:
        if not isinstance(item, Mapping):
            raise ValueError("recovery record snapshot is invalid")
        package_id = item.get("package_id")
        version = item.get("version")
        enabled = item.get("enabled")
        if not isinstance(package_id, str) or not isinstance(version, str) or not isinstance(enabled, bool):
            raise ValueError("recovery record snapshot is invalid")
        entry: dict[str, Any] = {"package_id": package_id, "version": version, "enabled": enabled}
        status = item.get("status")
        if isinstance(status, str) and status in _STATUSES:
            entry["status"] = status
        for key in ("bundle_id", "bundle_version"):
            value = item.get(key)
            if isinstance(value, str) and value.strip():
                entry[key] = value
        result.append(entry)
    return result


def _public_record(record: Mapping[str, Any]) -> dict[str, Any]:
    value = {
        "id": record.get("id", ""),
        "timestamp": record.get("timestamp", ""),
        "action": record.get("action", ""),
        "package_ids": record.get("package_ids", []),
    }
    source_record_id = record.get("source_record_id")
    if isinstance(source_record_id, str):
        value["source_record_id"] = source_record_id
    return value


def _non_empty(value: Any) -> bool:
    """Return whether a snapshot field holds a non-empty string."""
    return isinstance(value, str) and bool(value)


def _expected_states(record: Mapping[str, Any]) -> dict[tuple[str, ...], dict[str, Any]]:
    """Return the recorded state of each package copy.

    Standalone copies are keyed by ``(package_id, version)``; bundle members also
    carry their container identity, because the same id and version may exist in
    a standalone copy and in one or more bundle copies with different states.
    """
    result: dict[tuple[str, ...], dict[str, Any]] = {}
    for item in _snapshot(record):
        bundle_id = item.get("bundle_id")
        bundle_version = item.get("bundle_version")
        if _non_empty(bundle_id) and _non_empty(bundle_version):
            result[(bundle_id, bundle_version, item["package_id"], item["version"])] = item
        else:
            result[(item["package_id"], item["version"])] = item
    return result


def _read_manifest(root: Path) -> dict[str, Any] | None:
    """Read one managed manifest without following a symlink."""
    manifest_path = Path(root) / "manifest.json"
    if manifest_path.is_symlink() or not manifest_path.is_file():
        return None
    try:
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return document if isinstance(document, dict) else None


def _public_package_state(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Return the snapshot fields for one scanned copy."""
    value: dict[str, Any] = {
        "package_id": entry["package_id"],
        "version": entry["version"],
        "enabled": entry["enabled"],
    }
    status = entry.get("status")
    if isinstance(status, str) and status in _STATUSES:
        value["status"] = status
    for key in ("bundle_id", "bundle_version"):
        text = entry.get(key)
        if isinstance(text, str) and text:
            value[key] = text
    return value


def _standalone_entry(
    install_dir: Path,
    package_id: str,
    version_dir: Path,
    document: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one standalone copy with its ledger state."""
    if version_dir.is_symlink() or not version_dir.is_dir() or version_dir.name.startswith("."):
        return None
    try:
        root = version_dir.resolve()
        if not root.is_relative_to(install_dir):
            return None
    except OSError:
        return None
    if document.get("package_type") != "extension":
        return None
    package = document.get("package")
    if not isinstance(package, Mapping):
        return None
    if package.get("id") != package_id or package.get("version") != version_dir.name:
        return None
    record = state.get("packages", {}).get((package_id, version_dir.name))
    enabled = _record_enabled(record, document)
    active = record.get("active") if isinstance(record, Mapping) and isinstance(record.get("active"), bool) else enabled
    return {
        "package_id": package_id,
        "version": version_dir.name,
        "enabled": enabled,
        "status": _state_status(record, enabled, active, member=False),
        "manifest_path": str(root / "manifest.json"),
    }


def _member_entry(
    install_dir: Path,
    bundle_id: str,
    bundle_version: str,
    bundle_root: Path,
    member_dir: Path,
    document: Mapping[str, Any],
    state: Mapping[str, Any],
) -> dict[str, Any] | None:
    """Return one bundle member copy with its ledger state."""
    try:
        root = member_dir.resolve()
        if not root.is_relative_to(install_dir):
            return None
        resolved_bundle = bundle_root.resolve()
        if not root.is_relative_to(resolved_bundle):
            return None
    except OSError:
        return None
    if document.get("package_type") != "extension":
        return None
    package = document.get("package")
    if not isinstance(package, Mapping):
        return None
    package_id = package.get("id")
    version = package.get("version")
    if not isinstance(package_id, str) or not package_id:
        return None
    if not isinstance(version, str) or not version:
        return None
    record = state.get("members", {}).get((bundle_id, bundle_version, package_id))
    enabled = _record_enabled(record, document)
    active = record.get("active") if isinstance(record, Mapping) and isinstance(record.get("active"), bool) else enabled
    return {
        "package_id": package_id,
        "version": version,
        "enabled": enabled,
        "status": _state_status(record, enabled, active, member=True),
        "manifest_path": str(root / "manifest.json"),
        "bundle_id": bundle_id,
        "bundle_version": bundle_version,
    }


def _record_enabled(record: Mapping[str, Any] | None, document: Mapping[str, Any]) -> bool:
    if isinstance(record, Mapping) and isinstance(record.get("enabled"), bool):
        return record["enabled"]
    return document.get("enable") if isinstance(document.get("enable"), bool) else True


def _state_status(record: Mapping[str, Any] | None, enabled: bool, active: bool, *, member: bool) -> str:
    if isinstance(record, Mapping):
        status = record.get("status")
        if isinstance(status, str) and status in _STATUSES:
            return status
    if not enabled:
        return "disabled"
    return "standby" if not active else ("pending" if member else "active")


def _target_state(
    entry: Mapping[str, Any],
    expected: Mapping[tuple[str, ...], Mapping[str, Any]],
) -> dict[str, Any]:
    """Return the restored state for one copy; missing copies are disabled."""
    if entry.get("bundle_id"):
        expected_entry = expected.get((
            entry["bundle_id"], entry.get("bundle_version"),
            entry["package_id"], entry["version"],
        ))
        if expected_entry is None:
            # Records created before bundle-member scanning stored the bundle root
            # only. Only a recorded disable is safe to project onto its members:
            # the old scanner defaulted a root without ``enable`` to enabled, so
            # projecting that value would resurrect disabled members.
            legacy = expected.get((entry["bundle_id"], entry.get("bundle_version")))
            if legacy is not None and legacy["enabled"] is False:
                expected_entry = legacy
    else:
        expected_entry = expected.get((entry["package_id"], entry["version"]))
    if expected_entry is None:
        return {"enabled": False, "active": False, "status": "disabled"}
    if expected_entry["enabled"] is not True:
        return {"enabled": False, "active": False, "status": "disabled"}
    recorded = expected_entry.get("status")
    if entry.get("bundle_id"):
        # Members are loaded by the container reconciliation on the next start:
        # a pending row makes both hosts actually load the copy, whereas an
        # active row would be treated as "already active but not loaded".
        return {
            "enabled": True,
            "active": False,
            "status": "standby" if recorded == "standby" else "pending",
        }
    if recorded == "standby":
        return {"enabled": True, "active": False, "status": "standby"}
    return {"enabled": True, "active": True, "status": "active"}


def _apply_state_record(record: dict[str, Any], target: Mapping[str, Any]) -> bool:
    """Write one ledger row and return whether it changed."""
    if record.get("status") == "uninstalling":
        # Keep the tombstone for the host that still has to remove the copy.
        return False
    changed = any(record.get(key) != target[key] for key in ("enabled", "active", "status"))
    record["enabled"] = target["enabled"]
    record["active"] = target["active"]
    record["status"] = target["status"]
    if record.get("error"):
        record["error"] = ""
    return changed


def _derive_bundle_record(bundle: dict[str, Any]) -> bool:
    """Recompute a container's status from the member rows just written."""
    members = [item for item in bundle.get("member_records") or () if isinstance(item, Mapping)]
    enabled = [item for item in members if item.get("enabled") is True]
    if not enabled:
        active, status = False, "disabled"
    else:
        active = True
        if any(item.get("status") == "pending" for item in enabled):
            status = "pending"
        elif all(item.get("status") == "active" and item.get("active") is True for item in enabled):
            status = "active"
        else:
            status = "partial"
    changed = bundle.get("active") is not active or bundle.get("status") != status
    bundle["active"] = active
    bundle["status"] = status
    if not active and bundle.get("error"):
        bundle["error"] = ""
    return changed


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        temporary.write_text(content, encoding="utf-8")
        # Windows can reject the replace while another reader holds the file.
        for attempt in range(5):
            try:
                os.replace(temporary, path)
                break
            except OSError:
                if attempt == 4:
                    raise
                time.sleep(0.02 * (attempt + 1))
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _valid_session_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 128 and bool(value.strip())


def _resolve_directory(key: str, env_name: str, default: Path) -> Path:
    """Resolve an extension directory without importing an extension runtime."""
    value = _extension_config_value(key)
    if not value:
        value = os.getenv(env_name, "").strip()
    if not value:
        return default
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    from jiuwenswarm.common.paths import get_user_workspace_dir

    return get_user_workspace_dir() / path


def _extension_config_value(key: str) -> str:
    try:
        from jiuwenswarm.common.config import get_config

        config = get_config()
    except Exception:  # noqa: BLE001 - recovery must work with a damaged main configuration
        return ""
    extensions = config.get("extensions") if isinstance(config, Mapping) else None
    value = extensions.get(key) if isinstance(extensions, Mapping) else None
    return value.strip() if isinstance(value, str) else ""
