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
from pathlib import Path
from typing import Any, Iterator, Mapping

import portalocker

_HISTORY_FILE = "system_plugin_recovery.json"
_ENDPOINT_FILE = "system_plugin_recovery_endpoint.json"
_RESTART_FILE = "system_plugin_recovery_restart.json"
_LOCK_FILE = "system_plugin_recovery.lock"
_MAX_RECORDS = 50
_SCHEMA_VERSION = 1
_RESTART_DELAY_SECONDS = 1.0
_INSTALL_DIR_ENV = "EXTENSION_INSTALL_DIR"
_DATA_DIR_ENV = "EXTENSION_DATA_DIR"
_RESTART_SIGNAL_PATH_ENV = "JIUWENSWARM_RECOVERY_RESTART_SIGNAL_PATH"
_INSTALL_DIR_DEFAULT = "extension_pkgs"
_DATA_DIR_DEFAULT = "extension"


class RecoveryRecordNotFound(LookupError):
    """Raised when a requested history record is no longer retained."""


class SystemPluginRecovery:
    """Record package states and restore a retained state without loading code."""

    def __init__(self, install_dir: Path | None = None, data_dir: Path | None = None) -> None:
        resolved_install_dir = install_dir or _resolve_directory(
            "install_dir",
            _INSTALL_DIR_ENV,
            _INSTALL_DIR_DEFAULT,
        )
        resolved_data_dir = data_dir or _resolve_directory(
            "data_dir",
            _DATA_DIR_ENV,
            _DATA_DIR_DEFAULT,
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
            self._write_history(document)
            return _public_record(record)

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
            expected = _expected_enabled_states(source)
            return [
                {
                    "package_id": state["package_id"],
                    "version": state["version"],
                    "enabled": expected.get((state["package_id"], state["version"]), False),
                }
                for state in self._scan_packages()
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
            expected = _expected_enabled_states(source)
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
        packages: list[dict[str, Any]] = []
        if not self.install_dir.is_dir():
            return packages
        for package_dir in sorted(self.install_dir.iterdir(), key=lambda path: path.name):
            if package_dir.is_symlink() or not package_dir.is_dir() or package_dir.name.startswith("."):
                continue
            for version_dir in sorted(package_dir.iterdir(), key=lambda path: path.name):
                item = _package_state(self.install_dir, package_dir.name, version_dir)
                if item is not None:
                    packages.append(item)
        return packages

    def _apply_snapshot(self, expected: dict[tuple[str, str], bool]) -> list[str]:
        pending: list[tuple[str, Path, dict[str, Any], dict[str, Any]]] = []
        if not self.install_dir.is_dir():
            return []
        for package_dir in sorted(self.install_dir.iterdir(), key=lambda path: path.name):
            if package_dir.is_symlink() or not package_dir.is_dir() or package_dir.name.startswith("."):
                continue
            for version_dir in sorted(package_dir.iterdir(), key=lambda path: path.name):
                state = _package_state(self.install_dir, package_dir.name, version_dir)
                if state is None:
                    continue
                key = (state["package_id"], state["version"])
                enabled = expected.get(key, False)
                if state["enabled"] == enabled:
                    continue
                manifest_path = version_dir / "manifest.json"
                try:
                    document = json.loads(manifest_path.read_text(encoding="utf-8"))
                except (OSError, UnicodeError, json.JSONDecodeError) as error:
                    raise ValueError("managed manifest is invalid") from error
                if not isinstance(document, dict):
                    raise ValueError("managed manifest is invalid")
                target = dict(document)
                target["enable"] = enabled
                pending.append((state["package_id"], manifest_path, document, target))

        written: list[tuple[Path, dict[str, Any]]] = []
        try:
            for _, manifest_path, original, target in pending:
                _write_json(manifest_path, target)
                written.append((manifest_path, original))
        except (OSError, TypeError, ValueError):
            for manifest_path, original in reversed(written):
                try:
                    _write_json(manifest_path, original)
                except (OSError, TypeError, ValueError):
                    pass
            raise
        return [package_id for package_id, _, _, _ in pending]


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
        result.append({"package_id": package_id, "version": version, "enabled": enabled})
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


def _expected_enabled_states(record: Mapping[str, Any]) -> dict[tuple[str, str], bool]:
    """Return the enabled state map stored in one recovery record."""
    return {
        (item["package_id"], item["version"]): item["enabled"]
        for item in _snapshot(record)
    }


def _package_state(
    install_dir: Path,
    package_id: str,
    version_dir: Path,
) -> dict[str, Any] | None:
    if version_dir.is_symlink() or not version_dir.is_dir() or version_dir.name.startswith("."):
        return None
    try:
        root = version_dir.resolve()
        if not root.is_relative_to(install_dir):
            return None
        manifest_path = root / "manifest.json"
        if manifest_path.is_symlink() or not manifest_path.is_file():
            return None
        document = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    if not isinstance(document, dict) or document.get("package_type") != "extension":
        return None
    package = document.get("package")
    if not isinstance(package, Mapping):
        return None
    manifest_id = package.get("id")
    manifest_version = package.get("version")
    if manifest_id != package_id or manifest_version != version_dir.name:
        return None
    return {
        "package_id": package_id,
        "version": version_dir.name,
        "enabled": document.get("enable")
        if isinstance(document.get("enable"), bool)
        else True,
    }


def _write_json(path: Path, document: Mapping[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        content = json.dumps(document, ensure_ascii=False, indent=2) + "\n"
        temporary.write_text(content, encoding="utf-8")
        os.replace(temporary, path)
    except OSError:
        temporary.unlink(missing_ok=True)
        raise


def _valid_session_id(value: object) -> bool:
    return isinstance(value, str) and len(value) <= 128 and bool(value.strip())


def _resolve_directory(key: str, env_name: str, default: str) -> Path:
    """Resolve an extension directory without importing an extension runtime."""
    value = _extension_config_value(key)
    if not value:
        value = os.getenv(env_name, "").strip() or default
    path = Path(value).expanduser()
    if path.is_absolute():
        return path
    from jiuwenswarm.common.utils import get_user_workspace_dir

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
