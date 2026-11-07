# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Host-mediated file import and export for application UI packages (S_10 sections 6 and 7).

A package never sees a path on the user's machine. The browser uploads the
files the user picked under a host-issued ticket; the Gateway writes them into
the calling package's data directory and hands the package a relative path.
Exports go the other way: a package names a file in its own data directory and
the host gives that one browser connection a single-use download address.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
import time
from collections.abc import AsyncIterator, Callable, Mapping
from contextlib import ExitStack
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path, PurePosixPath
from typing import TYPE_CHECKING, Any
from uuid import uuid4

from openjiuwen.core.extension import ExtensionFailure
from openjiuwen.core.extension.data.files import run_file_operation

from jiuwenswarm.gateway.extensions_v2.webui.host_settings import CODE_BAD_REQUEST, HostRequestError
from jiuwenswarm.gateway.extensions_v2.webui.models import RenderTarget, valid_instance_key

if TYPE_CHECKING:
    from jiuwenswarm.gateway.extensions_v2.runtime import GatewayRuntime

logger = logging.getLogger(__name__)

UPLOAD_PREFIX = "/extension-uploads/"
DOWNLOAD_PREFIX = "/extension-downloads/"
#: Host-managed directory inside each package data directory.
UPLOAD_DIR = ".uploads"
UPLOAD_TTL_S = 600.0
DOWNLOAD_TTL_S = 300.0
MAX_UPLOAD_FILES = 1000
MAX_OPEN_UPLOADS = 4
MAX_PATH_BYTES = 512
MAX_SEGMENT_BYTES = 255
MAX_PATH_DEPTH = 16
MAX_FILENAME_BYTES = 255
CODE_TARGET_INACTIVE = "TARGET_INACTIVE"
CODE_UPLOAD_TOO_LARGE = "UPLOAD_TOO_LARGE"
TOKEN = re.compile(r"^[0-9a-f]{32}$")
_FORBIDDEN_CHARACTERS = frozenset('\\<>:"|?*')


def _segment_ok(segment: str) -> bool:
    return (0 < len(segment.encode("utf-8")) <= MAX_SEGMENT_BYTES and segment not in (".", "..")
            and not any(character in _FORBIDDEN_CHARACTERS or ord(character) < 32 or ord(character) == 127
                        for character in segment))


def validate_manifest(props: Mapping[str, Any], files: Any, max_bytes: int) -> dict[str, int]:
    """Check the picked files against the FileImport node and the size limit."""
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_UPLOAD_FILES:
        raise HostRequestError(CODE_BAD_REQUEST, f"files must list 1 to {MAX_UPLOAD_FILES} entries")
    directory = props.get("directory") is True
    if not directory and not props.get("multiple") and len(files) != 1:
        raise HostRequestError(CODE_BAD_REQUEST, "this import takes one file")
    accept = [item.lower() for item in props.get("accept", ())]
    result: dict[str, int] = {}
    folded: set[str] = set()
    roots: set[str] = set()
    for item in files:
        if not isinstance(item, Mapping) or set(item) != {"path", "size"}:
            raise HostRequestError(CODE_BAD_REQUEST, "each file has path and size")
        path, size = item["path"], item["size"]
        if type(size) is not int or size < 0:
            raise HostRequestError(CODE_BAD_REQUEST, "file size must be a non-negative integer")
        segments = path.split("/") if isinstance(path, str) else []
        if (not segments or len(path.encode("utf-8")) > MAX_PATH_BYTES or len(segments) > MAX_PATH_DEPTH
                or not all(_segment_ok(segment) for segment in segments)):
            raise HostRequestError(CODE_BAD_REQUEST, "invalid file path")
        if directory != (len(segments) > 1):
            raise HostRequestError(CODE_BAD_REQUEST, "file paths do not match the import mode")
        if accept and not any(segments[-1].lower().endswith(suffix) for suffix in accept):
            raise HostRequestError(CODE_BAD_REQUEST, "file type is not accepted")
        key = path.casefold()
        if key in folded:
            raise HostRequestError(CODE_BAD_REQUEST, "file paths must be unique")
        folded.add(key)
        roots.add(segments[0].casefold())
        result[path] = size
    prefixes = {"/".join(path.casefold().split("/")[:depth]) for path in result for depth in range(1, path.count("/") + 1)}
    if prefixes & folded or (directory and len(roots) != 1):
        raise HostRequestError(CODE_BAD_REQUEST, "file paths conflict")
    if sum(result.values()) > max_bytes:
        raise HostRequestError(CODE_UPLOAD_TOO_LARGE, f"the import exceeds {max_bytes} bytes")
    return result


def clean_filename(value: str) -> str:
    """Remove separators and control characters from a download name."""
    text = "".join(character for character in value
                   if character not in "/\\" and ord(character) >= 32 and ord(character) != 127).strip()
    if not text or text in (".", "..") or len(text.encode("utf-8")) > MAX_FILENAME_BYTES:
        raise ValueError("invalid download filename")
    return text


@dataclass
class Upload:
    """One open import ticket."""

    upload_id: str
    connection_id: str
    package_id: str
    generation: str
    target: RenderTarget
    node_key: str
    files: dict[str, int]
    directory: Path
    created: float
    received: set[str] = field(default_factory=set)
    receiving: set[str] = field(default_factory=set)

    def descriptor(self) -> dict[str, Any]:
        return {
            "upload_id": self.upload_id, "path": f"{UPLOAD_DIR}/{self.upload_id}",
            "total_bytes": sum(self.files.values()),
            "files": [{"path": path, "size": size} for path, size in self.files.items()],
        }


@dataclass(frozen=True)
class Download:
    """One offered export."""

    token: str
    connection_id: str
    package_id: str
    generation: str
    path: Path
    filename: str
    created: float


class TransferManager:
    """Own import and download tickets and the temporary import directories."""

    def __init__(self, runtime: GatewayRuntime, *, clock: Callable[[], float] = time.monotonic) -> None:
        self.runtime = runtime
        self.uploads: dict[str, Upload] = {}
        self.downloads: dict[str, Download] = {}
        self._clock = clock

    # ── import ────────────────────────────────────────────────────────────

    async def begin_upload(self, request) -> dict[str, Any]:
        """Validate one ``webui.upload.begin`` and issue its ticket (S_10 section 6.2)."""
        from jiuwenswarm.gateway.extensions_v2.webui.bridge import SLOTS, tree_nodes

        params = dict(request.params)
        target = params.get("target")
        if (set(params) != {"target", "node_key", "revision", "files"} or not isinstance(target, Mapping)
                or set(target) != {"scope", "slot_id", "package_id", "instance_key"}
                or target.get("scope") != "application" or target.get("slot_id") not in SLOTS
                or not isinstance(target.get("package_id"), str) or not isinstance(params["node_key"], str)
                or not valid_instance_key(target.get("instance_key"))):
            raise HostRequestError(CODE_BAD_REQUEST, "invalid upload target")
        browser = self.runtime.browser
        package_id = target["package_id"]
        scoped = RenderTarget(session_id="", slot_id=target["slot_id"], package_id=package_id,
                              instance_key=target["instance_key"], scope="application",
                              connection_id=request.connection_id)
        record = self.runtime.registry.get(scoped)
        node = tree_nodes(record.root).get(params["node_key"]) if record and record.root is not None else None
        generation = browser.generations.get(package_id)
        if (browser.active_application_package(package_id) is None or generation is None or node is None
                or type(params["revision"]) is not int or params["revision"] != record.revision
                or node.component != "FileImport" or node.props.get("disabled") is True
                or "ui.change" not in (node.events or {})):
            raise HostRequestError(CODE_TARGET_INACTIVE, "the import control is not current")
        files = validate_manifest(node.props, params["files"], self.runtime.config.upload_max_bytes)

        def target_is_current() -> bool:
            return (browser.generations.get(package_id) == generation
                    and self.runtime.registry.get(scoped) is record and record.revision == params["revision"])

        try:
            with self.runtime.package_io_call(package_id, generation):
                return await self._prepare_upload(scoped, params["node_key"], generation, files, target_is_current)
        except ExtensionFailure as exc:
            raise HostRequestError(CODE_TARGET_INACTIVE, "the import activation has ended") from exc

    async def _prepare_upload(self, target: RenderTarget, node_key: str, generation: str,
                              files: dict[str, int], target_is_current: Callable[[], bool]) -> dict[str, Any]:
        """Prepare one ticket under the caller's file-work lease, including cancellation cleanup.

        Records change synchronously and the new ticket is counted before the only await (creating its
        directory), so concurrent requests of one connection cannot exceed the limit, and the target
        validated by the caller is still current here.
        """
        self.expire()
        own = sorted((item for item in self.uploads.values() if item.connection_id == target.connection_id),
                     key=lambda item: item.created)
        stale = own[:max(0, len(own) - MAX_OPEN_UPLOADS + 1)]
        for item in stale:
            self.uploads.pop(item.upload_id, None)
        upload_id = uuid4().hex
        data_dir = self.runtime.package_data_dir(target.package_id)
        directory = data_dir / UPLOAD_DIR / upload_id
        upload = Upload(
            upload_id=upload_id, connection_id=target.connection_id, package_id=target.package_id,
            generation=generation, target=target, node_key=node_key,
            files=files, directory=directory, created=self._clock(),
        )
        self.uploads[upload_id] = upload
        for item in stale:
            self._discard_later(item)
        try:
            await run_file_operation(partial(directory.mkdir, parents=True))
        except BaseException:
            self._forget(upload)
            await self._discard(upload)
            raise
        unusable = (data_dir / UPLOAD_DIR).is_symlink() or not directory.resolve().is_relative_to(data_dir.resolve())
        if unusable or self.uploads.get(upload_id) is not upload or not target_is_current():
            # The original target may change while directories are prepared; never adopt its replacement.
            self._forget(upload)
            await self._discard(upload)
            if unusable:
                raise HostRequestError(CODE_BAD_REQUEST, "package data directory is not usable")
            raise HostRequestError(CODE_TARGET_INACTIVE, "the import was withdrawn before it was ready")
        return {"upload_id": upload_id, "url": f"{UPLOAD_PREFIX}{upload_id}/"}

    async def receive(self, upload_id: str, relative: str, chunks: AsyncIterator[bytes]) -> int:
        """Store one declared file of an open ticket; return the HTTP status (S_10 section 6.3).

        Access comes from the ticket alone, so a remote browser holding it may upload.
        """
        self.expire()
        upload = self.uploads.get(upload_id) if TOKEN.fullmatch(upload_id or "") else None
        if upload is None or relative not in upload.files:
            return 404
        if relative in upload.received or relative in upload.receiving:
            return 409
        try:
            with self.runtime.package_io_call(upload.package_id, upload.generation):
                return await self._receive_file(upload, relative, chunks)
        except ExtensionFailure:
            return 404

    async def _receive_file(self, upload: Upload, relative: str, chunks: AsyncIterator[bytes]) -> int:
        """Own the handle before opening it, and retain ownership until close and cleanup finish."""
        upload.receiving.add(relative)
        destination = upload.directory.joinpath(*PurePosixPath(relative).parts)
        partial_path = destination.with_name(f".{destination.name}.{uuid4().hex}.part")
        expected = upload.files[relative]
        status = 204
        resources = ExitStack()
        try:
            await run_file_operation(partial(destination.parent.mkdir, parents=True, exist_ok=True))
            if not destination.parent.resolve().is_relative_to(upload.directory.resolve()):
                return 404
            total = 0
            try:
                # The worker registers the handle before returning it. Cancellation cannot lose that owner.
                handle = await run_file_operation(lambda: resources.enter_context(open(partial_path, "xb")))
                async for chunk in chunks:
                    total += len(chunk)
                    if total > expected:
                        status = 413
                        break
                    await run_file_operation(partial(handle.write, chunk))
            finally:
                await run_file_operation(resources.close)
            if status == 204 and total != expected:
                status = 400
            if status == 204 and self.uploads.get(upload.upload_id) is upload:
                await run_file_operation(partial(os.replace, partial_path, destination))
                upload.received.add(relative)
            elif status == 204:
                status = 404
        except OSError as exc:
            logger.warning("[GatewayExtensions] import %s failed for %s: %s", upload.upload_id, relative, exc)
            status = 404 if self.uploads.get(upload.upload_id) is not upload else 500
        finally:
            try:
                await run_file_operation(partial(partial_path.unlink, missing_ok=True))
            finally:
                upload.receiving.discard(relative)
                if self.uploads.get(upload.upload_id) is not upload and not upload.receiving:
                    await self._discard(upload)
        return status

    def claim(self, upload_id: str, target: RenderTarget, node_key: str) -> Upload | None:
        """Consume a complete ticket that belongs to this connection, instance and node."""
        upload = self.uploads.get(upload_id)
        if (upload is None or self._clock() - upload.created > UPLOAD_TTL_S
                or upload.target.key != target.key or upload.connection_id != target.connection_id
                or upload.node_key != node_key or upload.receiving or upload.received != set(upload.files)
                or self.runtime.browser.generations.get(upload.package_id) != upload.generation):
            return None
        return self.uploads.pop(upload_id)

    async def discard(self, upload: Upload) -> None:
        """Delete an import directory; after a claim this removes what the package left."""
        await self._discard(upload)

    # ── export ────────────────────────────────────────────────────────────

    async def offer_download(self, package_id: str, generation: str, connection_id: str, path: Any,
                             filename: Any) -> dict[str, Any]:
        """Issue a single-use download of one data file to one connection (S_10 section 7.1)."""
        if not connection_id:
            raise ValueError("ops.ui.download requires a browser connection")
        if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
            raise ValueError("download path must be relative to the package data directory")
        data_dir = self.runtime.package_data_dir(package_id)
        resolved_root = data_dir.resolve()
        current = data_dir
        for part in PurePosixPath(path).parts:
            if part in ("", ".", "..") or "\\" in part:
                raise ValueError("invalid download path")
            current = current / part
            if current.is_symlink():
                raise ValueError("download path must not contain symlinks")
        if not current.resolve().is_relative_to(resolved_root) or not current.is_file():
            raise ValueError("download path is not a file in the package data directory")
        if filename is not None and not isinstance(filename, str):
            raise ValueError("invalid download filename")
        name = clean_filename(current.name if filename is None else filename)
        self.expire()
        token = uuid4().hex
        self.downloads[token] = Download(token=token, connection_id=connection_id, package_id=package_id,
                                         generation=generation, path=current, filename=name, created=self._clock())
        delivered = await self.runtime.webui.transport.send_download(connection_id, {
            "package_id": package_id, "url": f"{DOWNLOAD_PREFIX}{token}", "filename": name,
            "size": current.stat().st_size,
        })
        if not delivered:
            self.downloads.pop(token, None)
            return {"status": "delivery_failed"}
        return {"status": "offered"}

    def download(self, token: str, *, consume: bool) -> Download | None:
        """Return a valid offered download; a GET consumes it (S_10 section 7.2)."""
        if not TOKEN.fullmatch(token or ""):
            return None
        offer = self.downloads.get(token)
        if (offer is None or self._clock() - offer.created > DOWNLOAD_TTL_S
                or self.runtime.browser.generations.get(offer.package_id) != offer.generation
                or offer.path.is_symlink() or not offer.path.is_file()):
            return None
        if consume:
            self.downloads.pop(token, None)
        return offer

    # ── lifecycle ─────────────────────────────────────────────────────────

    def expire(self) -> None:
        """Drop expired tickets now and delete their directories in the background.

        Nothing awaits here, so callers keep the state they validated; uninstall waits for the
        background deletions before it removes the package data (``BrowserBridge.settle``).
        """
        now = self._clock()
        expired = [item for item in self.uploads.values() if now - item.created > UPLOAD_TTL_S]
        for upload in expired:
            self.uploads.pop(upload.upload_id, None)
            self._discard_later(upload)
        for token in [key for key, item in self.downloads.items() if now - item.created > DOWNLOAD_TTL_S]:
            self.downloads.pop(token, None)

    def release(self, *, connection_id: str | None = None, package_id: str | None = None) -> list[Upload]:
        """Invalidate the tickets of a closed connection or a released package."""
        def matches(item) -> bool:
            return item.connection_id == connection_id if connection_id is not None else item.package_id == package_id
        dropped = [self.uploads.pop(key) for key, item in list(self.uploads.items()) if matches(item)]
        for token, item in list(self.downloads.items()):
            if matches(item):
                self.downloads.pop(token, None)
        return dropped

    async def purge(self, package_ids) -> None:
        """Remove import directories left by an earlier process."""
        for package_id in package_ids:
            try:
                directory = self.runtime.package_data_dir(package_id) / UPLOAD_DIR
            except ValueError:
                continue
            if directory.is_symlink():
                await run_file_operation(partial(directory.unlink, missing_ok=True))
            elif directory.exists():
                await run_file_operation(partial(shutil.rmtree, directory, True))

    def _forget(self, upload: Upload) -> None:
        """Drop this ticket's record unless something else replaced or removed it."""
        if self.uploads.get(upload.upload_id) is upload:
            del self.uploads[upload.upload_id]

    def _discard_later(self, upload: Upload) -> None:
        self.runtime.browser.spawn(self._discard(upload))

    async def _discard(self, upload: Upload) -> None:
        await run_file_operation(partial(shutil.rmtree, upload.directory, True))

    def clear(self) -> None:
        self.uploads.clear()
        self.downloads.clear()
