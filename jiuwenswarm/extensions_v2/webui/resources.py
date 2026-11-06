# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Publish plugin-owned files with activation and session-scoped lifetimes."""

from __future__ import annotations

import asyncio
import hashlib
import secrets
import shutil
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import TypeVar
from urllib.parse import quote

from openjiuwen.core.events import EventHandlerOps

from jiuwenswarm.common.webui_resources import (
    MAX_RESOURCE_BYTES,
    MAX_RESOURCE_FILES,
    MAX_RESOURCE_SET_BYTES,
    RESOURCE_MIME_TYPES,
    RESOURCE_PREFIX,
    decode_resource_route,
    encode_resource_route,
    resource_path,
)
from jiuwenswarm.extensions_v2.webui.call_context import current_connection
from jiuwenswarm.extensions_v2.webui.contributions import WebUICatalogService

_T = TypeVar("_T")


@dataclass(slots=True)
class _Publication:
    package_id: str
    generation: int
    route: str
    root: Path
    managed: bool
    files: dict[str, str] | None = None
    ready: bool = False
    revoked: bool = False
    writer: asyncio.Task | None = None
    idle: asyncio.Event = field(default_factory=asyncio.Event)

    def __post_init__(self) -> None:
        self.idle.set()


@dataclass(frozen=True, slots=True)
class WebUIResourceDirectory:
    """Host-owned output directory; the plugin writes its files inside this context."""

    resource_id: str
    path: Path
    _service: WebUIResourceService = field(repr=False)
    _publication: _Publication = field(repr=False)

    async def __aenter__(self) -> WebUIResourceDirectory:
        await self._service.begin_write(self.resource_id, self._publication)
        return self

    async def __aexit__(self, exc_type: object, exc: object, traceback: object) -> None:
        self._publication.ready = exc_type is None
        self._publication.writer = None
        self._publication.idle.set()


class WebUIResourceService:
    """Allocate output directories and serve registered files without generating content."""

    def __init__(self, catalog: WebUICatalogService) -> None:
        self._catalog = catalog
        self._directory: TemporaryDirectory | None = None
        self._sets: dict[str, _Publication] = {}
        self._lock = asyncio.Lock()
        self._closed_sessions: set[str] = set()
        self._closed_connections: set[str] = set()
        self._closed = False
        self._revoked_activations: dict[str, object] = {}

    def _owner(self, ops: EventHandlerOps) -> tuple[str, int, str]:
        metadata = ops.package_metadata
        record = self._catalog.active_package(metadata.package_id) if metadata is not None else None
        if (record is None or self._revoked_activations.get(record.package_id) is record
                or metadata.root != record.root or metadata.package_version != record.version):
            raise PermissionError("Active package identity is required for WebUI files")
        context = ops.session_context
        connection = current_connection()
        session_id = ""
        connection_id = ""
        user_id = ""
        if context is not None:
            session_id = context.session_id
            user_id = str(context.metadata.get("user_id") or "")
        if not session_id and connection is not None:
            connection_id = connection.connection_id
            user_id = connection.user_id
        if (self._closed or session_id in self._closed_sessions or connection_id in self._closed_connections
                or not (session_id or connection_id) or (not session_id and connection is not None and connection.closed)):
            raise PermissionError("An active WebUI session or connection is required")
        route = encode_resource_route(user_id, session_id, connection_id)
        decode_resource_route(route)
        return record.package_id, record.generation, route

    def _owned(self, ops: EventHandlerOps, resource_id: str) -> _Publication:
        package_id, generation, route = self._owner(ops)
        item = self._sets.get(resource_id)
        if item is None or item.revoked or (item.package_id, item.generation) != (package_id, generation):
            raise PermissionError("WebUI resource does not belong to this activation")
        user_id, session_id, connection_id = decode_resource_route(route)
        owner_user, owner_session, owner_connection = decode_resource_route(item.route)
        if ((session_id, connection_id) != (owner_session, owner_connection)
                or (user_id and user_id != owner_user)):
            raise PermissionError("WebUI resource scope does not match")
        return item

    async def create_directory(self, ops: EventHandlerOps, *, previous: str = "") -> WebUIResourceDirectory:
        """Allocate a private output directory bound to the caller's current UI scope."""
        async with self._lock:
            package_id, generation, route = self._owner(ops)
            if previous:
                route = self._owned(ops, previous).route
            scope = decode_resource_route(route)[1:]
            count = sum(item.managed and item.package_id == package_id and item.generation == generation
                        and decode_resource_route(item.route)[1:] == scope for item in self._sets.values())
            if count >= 32:
                raise ValueError("Too many unreleased WebUI resource directories")
            if self._directory is None:
                self._directory = TemporaryDirectory(prefix="jiuwenswarm-webui-")
            resource_id = secrets.token_urlsafe(32)
            directory = Path(self._directory.name) / resource_id
            try:
                await _io(directory.mkdir)
            except asyncio.CancelledError:
                await _io(lambda: _delete_directory(directory))
                raise
            try:
                if self._owner(ops)[:2] != (package_id, generation):
                    raise PermissionError("WebUI activation changed during directory creation")
            except BaseException:
                await _io(lambda: _delete_directory(directory))
                raise
            item = _Publication(package_id, generation, route, directory, True)
            self._sets[resource_id] = item
            return WebUIResourceDirectory(resource_id, directory, self, item)

    async def begin_write(self, resource_id: str, item: _Publication) -> None:
        """Acquire the directory's write lifetime before plugin file I/O starts."""
        async with self._lock:
            if self._sets.get(resource_id) is not item or not self._active(item):
                raise PermissionError("WebUI output directory is no longer active")
            if item.files is not None or not item.idle.is_set():
                raise ValueError("WebUI output directory is published or already being written")
            item.ready = False
            item.writer = asyncio.current_task()
            item.idle.clear()

    async def asset_url(self, ops: EventHandlerOps, path: str, *, resource_id: str = "") -> str:
        """Return an HTTP URL for a packaged asset or a completed plugin output directory."""
        resource_path(path)
        async with self._lock:
            if resource_id:
                item = self._owned(ops, resource_id)
                if not item.managed or not item.ready or not item.idle.is_set():
                    raise ValueError("Finish the WebUI directory write context before publishing")
                if item.files is None:
                    files = await _io(lambda: _snapshot_files(item.root))
                    try:
                        self._owned(ops, resource_id)
                    except BaseException:
                        await self._remove(resource_id)
                        raise
                    item.files = files
                if path not in item.files:
                    raise ValueError("File was not registered in the WebUI output directory")
            else:
                package_id, generation, route = self._owner(ops)
                record = self._catalog.active_package(package_id)
                root = record.root / "assets"
                await _io(lambda: _read_regular(root, path))
                if self._owner(ops) != (package_id, generation, route):
                    raise PermissionError("WebUI activation changed while reading an asset")
                match = next(((key, entry) for key, entry in self._sets.items()
                              if not entry.managed and not entry.revoked and (entry.package_id, entry.generation, entry.route)
                              == (package_id, generation, route)), None)
                if match is None:
                    resource_id = secrets.token_urlsafe(32)
                    item = _Publication(package_id, generation, route, root, False)
                    self._sets[resource_id] = item
                else:
                    resource_id, item = match
            return f"{RESOURCE_PREFIX}/{item.route}/{resource_id}/{quote(path, safe='/')}"

    async def release(self, ops: EventHandlerOps, resource_id: str) -> None:
        """Revoke a caller-owned resource and finish cleanup even when cancelled."""
        await complete_before_cancel(self._release(ops, resource_id, asyncio.current_task()))

    async def _release(self, ops: EventHandlerOps, resource_id: str, caller: asyncio.Task | None) -> None:
        async with self._lock:
            item = self._sets.get(resource_id)
            if item is None:
                return
            if not item.revoked:
                self._owned(ops, resource_id)
            if item.writer is caller:
                raise ValueError("Exit the directory write context before releasing it")
            item.revoked = True
        await self._retire([(resource_id, item)])

    def _active(self, item: _Publication) -> bool:
        record = self._catalog.active_package(item.package_id)
        _, session_id, connection_id = decode_resource_route(item.route)
        return (not self._closed and not item.revoked and record is not None and record.generation == item.generation
                and self._revoked_activations.get(item.package_id) is not record
                and session_id not in self._closed_sessions and connection_id not in self._closed_connections)

    async def read(self, resource_id: str, path: str, *, route: str) -> tuple[bytes, str]:
        """Read registered files while enforcing ownership, activation and integrity."""
        resource_path(path)
        async with self._lock:
            item = self._sets.get(resource_id)
            if (item is None or not self._active(item) or item.route != route
                    or (item.managed and (item.files is None or path not in item.files))):
                raise FileNotFoundError("WebUI resource not found")
            try:
                data = await _io(lambda: _read_regular(item.root, path))
            except (ValueError, OSError) as exc:
                raise FileNotFoundError("WebUI resource not found") from exc
            if item.managed and hashlib.sha256(data).hexdigest() != item.files[path]:
                raise FileNotFoundError("WebUI resource changed after publication")
            return data, RESOURCE_MIME_TYPES[Path(path).suffix.lower()]

    async def drop(self, *, package_id: str = "", session_id: str = "", connection_id: str = "") -> None:
        """Revoke a closed scope and wait for its active file writers before deleting output."""
        await complete_before_cancel(self._drop(package_id, session_id, connection_id))

    async def _drop(self, package_id: str, session_id: str, connection_id: str) -> None:
        async with self._lock:
            if package_id:
                record = self._catalog.active_package(package_id)
                if record is not None:
                    self._revoked_activations[package_id] = record
            if session_id:
                self._closed_sessions.add(session_id)
            if connection_id:
                self._closed_connections.add(connection_id)
            retired = []
            for resource_id, item in tuple(self._sets.items()):
                _, session, connection = decode_resource_route(item.route)
                if ((package_id and item.package_id == package_id) or (session_id and session == session_id)
                        or (connection_id and connection == connection_id)):
                    item.revoked = True
                    retired.append((resource_id, item))
        await self._retire(retired)

    async def shutdown(self) -> None:
        """Revoke all URLs and wait for writers before removing host-owned output."""
        await complete_before_cancel(self._shutdown())

    async def _shutdown(self) -> None:
        async with self._lock:
            self._closed = True
            retired = list(self._sets.items())
            for _, item in retired:
                item.revoked = True
        await self._retire(retired)
        async with self._lock:
            if self._directory is not None:
                await _io(self._directory.cleanup)
                self._directory = None

    async def _retire(self, items: list[tuple[str, _Publication]]) -> None:
        # Writers can finish or receive a rejection without waiting on the cleanup lock.
        for resource_id, item in items:
            await item.idle.wait()
            async with self._lock:
                if self._sets.get(resource_id) is item:
                    await self._remove(resource_id)

    async def _remove(self, resource_id: str) -> None:
        item = self._sets[resource_id]
        if item.managed:
            await _io(lambda: _delete_directory(item.root))
        del self._sets[resource_id]


async def _io(operation: Callable[[], _T]) -> _T:
    return await complete_before_cancel(asyncio.to_thread(operation))


async def complete_before_cancel(operation: Awaitable[_T]) -> _T:
    """Finish one operation before propagating cancellation of its caller.

    Further cancellation requests cannot interrupt the accepted operation. If
    it fails after cancellation, retain that failure as the cancellation cause.
    Cancellation of the operation itself remains a terminal result.

    Args:
        operation: The coroutine or future to run exactly once.

    Returns:
        The operation result when the caller was not cancelled.

    Raises:
        asyncio.CancelledError: The caller or operation was cancelled.
        Exception: The operation failed without caller cancellation.
    """
    task = asyncio.ensure_future(operation)
    cancelled: asyncio.CancelledError | None = None
    while True:
        try:
            result = await asyncio.shield(task)
        except asyncio.CancelledError as error:
            if task.cancelled():
                if cancelled is not None:
                    raise cancelled from error
                return task.result()
            if cancelled is None:
                cancelled = error
        except Exception as error:
            if cancelled is not None:
                raise cancelled from error
            raise
        else:
            if cancelled is not None:
                raise cancelled
            return result


def _delete_directory(path: Path) -> None:
    if path.is_symlink():
        path.unlink()
    elif path.exists():
        shutil.rmtree(path)


def _read_regular(root: Path, relative: str) -> bytes:
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Invalid WebUI asset directory")
    path = root
    for part in relative.split("/"):
        path = path / part
        if path.is_symlink():
            raise ValueError("WebUI asset symlinks are not allowed")
    if not path.is_file() or not path.resolve().is_relative_to(root.resolve()):
        raise ValueError("WebUI asset is outside its directory")
    with path.open("rb") as stream:
        data = stream.read(MAX_RESOURCE_BYTES + 1)
    if len(data) > MAX_RESOURCE_BYTES:
        raise ValueError("WebUI resource exceeds 1 MiB")
    return data


def _snapshot_files(root: Path) -> dict[str, str]:
    files: dict[str, str] = {}
    total = 0
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("WebUI asset symlinks are not allowed")
        if path.is_dir():
            continue
        relative = resource_path(path.relative_to(root).as_posix())
        if relative.casefold() in {name.casefold() for name in files}:
            raise ValueError("WebUI resource paths differ only by case")
        if len(files) >= MAX_RESOURCE_FILES:
            raise ValueError("WebUI resource directory exceeds 64 files")
        data = _read_regular(root, relative)
        total += len(data)
        if total > MAX_RESOURCE_SET_BYTES:
            raise ValueError("WebUI resource directory exceeds 4 MiB")
        files[relative] = hashlib.sha256(data).hexdigest()
    if not files:
        raise ValueError("WebUI resource directory is empty")
    return files
