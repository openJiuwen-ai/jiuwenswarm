# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""TTL-owned immutable download assets for authorized file delivery."""

from __future__ import annotations

import hashlib
import json
import logging
import os
import stat
import tempfile
import threading
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_ASSET_STATE_STAGED = "staged"
_ASSET_STATE_COMMITTED = "committed"
_ACTIVE_ASSET_STATES = frozenset({_ASSET_STATE_STAGED, _ASSET_STATE_COMMITTED})
_DEFAULT_SWEEP_INTERVAL_SECONDS = 30.0
_DEFAULT_ORPHAN_GRACE_SECONDS = 120.0
_DIGEST_CHUNK_SIZE = 1024 * 1024


def _default_asset_root() -> Path:
    configured = str(os.getenv("JIUWENSWARM_DOWNLOAD_ASSET_ROOT") or "").strip()
    if configured:
        return Path(configured).expanduser()
    return Path(tempfile.gettempdir()) / f"jiuwenswarm-download-assets-{os.getuid()}"


@dataclass(frozen=True)
class VerifiedDownloadAsset:
    """One immutable sealed file staged before its token becomes visible."""

    asset_id: str
    sealed_path: Path
    expires_at: float
    size_bytes: int
    content_digest: str


class VerifiedDownloadAssetOwner:
    """Own staged and committed sealed files until their token TTL expires."""

    def __init__(
        self,
        *,
        root: Path | str | None = None,
        now_fn: Callable[[], float] | None = None,
        sweep_interval_seconds: float = _DEFAULT_SWEEP_INTERVAL_SECONDS,
        orphan_grace_seconds: float = _DEFAULT_ORPHAN_GRACE_SECONDS,
        start_sweeper: bool = True,
    ) -> None:
        requested_root = Path(root or _default_asset_root()).expanduser()
        if requested_root.is_symlink():
            raise ValueError("download_asset_root_invalid")
        self.root = requested_root.absolute().resolve(strict=False)
        self._now_fn = now_fn or time.time
        self._sweep_interval_seconds = max(float(sweep_interval_seconds), 0.1)
        self._orphan_grace_seconds = max(float(orphan_grace_seconds), 1.0)
        self._start_sweeper = start_sweeper
        self._lock = threading.RLock()
        self._stop_event = threading.Event()
        self._sweeper: threading.Thread | None = None

    def stage(
        self,
        source_path: Path | str,
        *,
        file_name: str,
        expires_at: float,
    ) -> VerifiedDownloadAsset:
        """Atomically capture one stable delivery-time snapshot."""

        if expires_at <= self._now_fn():
            raise ValueError("download_asset_expired")
        source = Path(source_path).expanduser().absolute()
        self._ensure_root()
        self.prune()
        asset_id = uuid.uuid4().hex
        suffix = Path(file_name).suffix[:32]
        sealed_path = self.root / f"{asset_id}{suffix}"
        sidecar_path = self._sidecar_path(asset_id)
        try:
            actual_size, actual_digest = self._copy_verified_source(
                source,
                sealed_path,
            )
            payload = {
                "asset_id": asset_id,
                "sealed_path": sealed_path.as_posix(),
                "expires_at": float(expires_at),
                "size_bytes": actual_size,
                "content_digest": actual_digest,
                "state": _ASSET_STATE_STAGED,
            }
            self._write_sidecar_atomic(sidecar_path, payload)
        except (OSError, ValueError):
            self._safe_unlink(sidecar_path)
            self._safe_unlink(sealed_path)
            raise
        self._ensure_sweeper()
        return VerifiedDownloadAsset(
            asset_id=asset_id,
            sealed_path=sealed_path,
            expires_at=float(expires_at),
            size_bytes=actual_size,
            content_digest=actual_digest,
        )

    def commit(self, asset: VerifiedDownloadAsset) -> None:
        """Mark a staged asset delivered while preserving the same TTL owner."""

        with self._lock:
            sidecar_path = self._sidecar_path(asset.asset_id)
            payload = self._read_sidecar(sidecar_path)
            if not self._payload_matches_asset(payload, asset):
                raise ValueError("download_asset_registration_mismatch")
            if payload.get("state") not in _ACTIVE_ASSET_STATES:
                raise ValueError("download_asset_state_invalid")
            payload["state"] = _ASSET_STATE_COMMITTED
            self._write_sidecar_atomic(sidecar_path, payload)

    def revoke(self, asset: VerifiedDownloadAsset) -> None:
        """Remove an asset whose token has not become externally visible."""

        with self._lock:
            self._safe_unlink(self._sidecar_path(asset.asset_id))
            self._safe_unlink(asset.sealed_path)

    def is_active(
        self,
        *,
        asset_id: str,
        sealed_path: Path | str,
        expires_at: float,
        size_bytes: int,
        content_digest: str,
        now: float | None = None,
    ) -> bool:
        """Validate managed token claims against durable staged ownership."""

        current_time = self._now_fn() if now is None else float(now)
        if current_time > float(expires_at):
            return False
        normalized_id = _normalize_asset_id(asset_id)
        if not normalized_id:
            return False
        candidate = Path(sealed_path).expanduser().resolve(strict=False)
        if candidate.parent != self.root or candidate.name.startswith("."):
            return False
        try:
            payload = self._read_sidecar(self._sidecar_path(normalized_id))
        except (OSError, ValueError):
            return False
        try:
            return (
                payload.get("state") in _ACTIVE_ASSET_STATES
                and payload.get("asset_id") == normalized_id
                and payload.get("sealed_path") == candidate.as_posix()
                and float(payload.get("expires_at")) == float(expires_at)
                and int(payload.get("size_bytes")) == int(size_bytes)
                and payload.get("content_digest") == _normalize_digest(content_digest)
                and _regular_file_without_symlink(candidate)
            )
        except (TypeError, ValueError):
            return False

    def prune(self, *, now: float | None = None) -> None:
        """Recover registered assets and remove expired or old orphan files."""

        current_time = self._now_fn() if now is None else float(now)
        self._ensure_root()
        with self._lock:
            registered_names: set[str] = set()
            for sidecar_path in self.root.glob("*.json"):
                if sidecar_path.is_symlink():
                    self._safe_unlink(sidecar_path)
                    continue
                try:
                    payload = self._read_sidecar(sidecar_path)
                    sealed_path = Path(str(payload["sealed_path"])).resolve(
                        strict=False
                    )
                    valid_location = (
                        sealed_path.parent == self.root
                        and payload.get("asset_id") == sidecar_path.stem
                    )
                    expired = current_time > float(payload["expires_at"])
                    valid_state = payload.get("state") in _ACTIVE_ASSET_STATES
                except (KeyError, OSError, TypeError, ValueError):
                    valid_location = False
                    expired = True
                    valid_state = False
                    sealed_path = self.root / sidecar_path.stem
                if not valid_location or expired or not valid_state:
                    self._safe_unlink(sidecar_path)
                    if valid_location:
                        self._safe_unlink(sealed_path)
                    continue
                registered_names.add(sealed_path.name)

            orphan_cutoff = current_time - self._orphan_grace_seconds
            for candidate in self.root.iterdir():
                if candidate.name.startswith(".") or candidate.suffix == ".json":
                    continue
                if candidate.name in registered_names or candidate.is_symlink():
                    continue
                try:
                    if candidate.stat(follow_symlinks=False).st_mtime < orphan_cutoff:
                        self._safe_unlink(candidate)
                except OSError:
                    continue

    def close(self) -> None:
        """Stop the optional background sweeper."""

        self._stop_event.set()
        sweeper = self._sweeper
        if sweeper is not None and sweeper is not threading.current_thread():
            sweeper.join(timeout=1.0)

    def _ensure_root(self) -> None:
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or not self.root.is_dir():
            raise ValueError("download_asset_root_invalid")
        os.chmod(self.root, 0o700)

    def _ensure_sweeper(self) -> None:
        if not self._start_sweeper or self._sweeper is not None:
            return
        with self._lock:
            if self._sweeper is not None:
                return
            self._sweeper = threading.Thread(
                target=self._sweep_loop,
                name="verified-download-asset-sweeper",
                daemon=True,
            )
            self._sweeper.start()

    def _sweep_loop(self) -> None:
        while not self._stop_event.wait(self._sweep_interval_seconds):
            try:
                self.prune()
            except OSError:
                logger.exception("Failed to prune verified download assets")

    @staticmethod
    def _copy_verified_source(
        source: Path,
        destination: Path,
    ) -> tuple[int, str]:
        try:
            source_stat = source.stat(follow_symlinks=False)
        except OSError as exc:
            raise ValueError("download_asset_source_not_file") from exc
        if not stat.S_ISREG(source_stat.st_mode) or source.is_symlink():
            raise ValueError("download_asset_source_not_file")
        read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
        write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        source_fd = os.open(source, read_flags, 0o600)
        destination_fd: int | None = None
        try:
            opened_source_stat = os.fstat(source_fd)
            if not stat.S_ISREG(opened_source_stat.st_mode):
                raise ValueError("download_asset_source_not_file")
            destination_fd = os.open(destination, write_flags, 0o600)
            digest = hashlib.sha256()
            copied = 0
            while True:
                chunk = os.read(source_fd, _DIGEST_CHUNK_SIZE)
                if not chunk:
                    break
                _write_all(destination_fd, chunk)
                copied += len(chunk)
                digest.update(chunk)
            actual_digest = f"sha256:{digest.hexdigest()}"
            if copied != opened_source_stat.st_size:
                raise ValueError("download_asset_size_mismatch")
            os.fsync(destination_fd)
            os.chmod(destination, 0o400)
            return copied, actual_digest
        finally:
            os.close(source_fd)
            if destination_fd is not None:
                os.close(destination_fd)

    def _write_sidecar_atomic(
        self,
        sidecar_path: Path,
        payload: dict[str, Any],
    ) -> None:
        temp_path = self.root / f".{sidecar_path.stem}.{uuid.uuid4().hex}.tmp"
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
        file_descriptor = os.open(temp_path, flags, 0o600)
        try:
            encoded = json.dumps(
                payload,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            _write_all(file_descriptor, encoded)
            os.fsync(file_descriptor)
        finally:
            os.close(file_descriptor)
        try:
            os.replace(temp_path, sidecar_path)
            directory_fd = os.open(self.root, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            self._safe_unlink(temp_path)

    @staticmethod
    def _read_sidecar(sidecar_path: Path) -> dict[str, Any]:
        if not _regular_file_without_symlink(sidecar_path):
            raise ValueError("download_asset_sidecar_invalid")
        payload = json.loads(sidecar_path.read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("download_asset_sidecar_invalid")
        return payload

    @staticmethod
    def _payload_matches_asset(
        payload: dict[str, Any],
        asset: VerifiedDownloadAsset,
    ) -> bool:
        return (
            payload.get("asset_id") == asset.asset_id
            and payload.get("sealed_path") == asset.sealed_path.as_posix()
            and float(payload.get("expires_at")) == asset.expires_at
            and int(payload.get("size_bytes")) == asset.size_bytes
            and payload.get("content_digest") == asset.content_digest
        )

    def _sidecar_path(self, asset_id: str) -> Path:
        normalized_id = _normalize_asset_id(asset_id)
        if not normalized_id:
            raise ValueError("download_asset_id_invalid")
        return self.root / f"{normalized_id}.json"

    def _safe_unlink(self, path: Path) -> None:
        if path.parent.resolve(strict=False) != self.root:
            return
        try:
            path.unlink(missing_ok=True)
        except OSError:
            logger.warning("Failed to remove verified download asset path=%s", path)


def _normalize_asset_id(value: str) -> str:
    normalized = str(value or "").strip().lower()
    if len(normalized) != 32:
        return ""
    try:
        int(normalized, 16)
    except ValueError:
        return ""
    return normalized


def _normalize_digest(value: str) -> str:
    normalized = str(value or "").strip().lower()
    normalized = normalized.removeprefix("sha256:")
    if len(normalized) != 64:
        raise ValueError("download_asset_digest_invalid")
    try:
        int(normalized, 16)
    except ValueError as exc:
        raise ValueError("download_asset_digest_invalid") from exc
    return f"sha256:{normalized}"


def _regular_file_without_symlink(path: Path) -> bool:
    try:
        mode = path.stat(follow_symlinks=False).st_mode
    except OSError:
        return False
    return stat.S_ISREG(mode) and not path.is_symlink()


def _write_all(file_descriptor: int, data: bytes) -> None:
    view = memoryview(data)
    while view:
        written = os.write(file_descriptor, view)
        if written <= 0:
            raise OSError("download asset write made no progress")
        view = view[written:]


_SHARED_VERIFIED_DOWNLOAD_ASSET_OWNER = VerifiedDownloadAssetOwner()


def get_verified_download_asset_owner() -> VerifiedDownloadAssetOwner:
    """Return the process-local owner backed by the shared delivery root."""

    return _SHARED_VERIFIED_DOWNLOAD_ASSET_OWNER
