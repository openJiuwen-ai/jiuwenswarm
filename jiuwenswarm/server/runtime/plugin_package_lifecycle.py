"""Coordinate plugin replacement with active turns in the Runtime Worker."""

from __future__ import annotations

import asyncio
import hashlib
from collections import Counter
from collections.abc import Awaitable, Callable, Iterator
from contextlib import contextmanager
from pathlib import Path


class PluginPackageBusy(RuntimeError):
    """A plugin is executing or being replaced."""


_users: Counter[str] = Counter()
_writers: set[str] = set()
_owner_streams: dict[asyncio.Task, list[Callable[[], Awaitable[None]]]] = {}
_stream_cleanup_tasks: set[asyncio.Task] = set()


def _close_owner_streams(owner: asyncio.Task) -> None:
    callbacks = _owner_streams.pop(owner, [])

    async def close_streams() -> None:
        # Parent streams close their children; never close nested generators concurrently.
        for close in callbacks:
            try:
                await close()
            except Exception as exc:
                owner.get_loop().call_exception_handler({
                    "message": "Abandoned plugin stream cleanup failed; reservation retained",
                    "exception": exc,
                })

    cleanup = owner.get_loop().create_task(close_streams())
    _stream_cleanup_tasks.add(cleanup)
    cleanup.add_done_callback(_stream_cleanup_tasks.discard)


def _package_key(name: str) -> str:
    from jiuwenswarm.server.runtime import extension_package_manager as catalog

    return str((catalog.get_local_root("plugin_packages") / name).resolve())


def package_fingerprint(path: Path) -> str:
    """Hash editable package contents, excluding interpreter-generated caches."""
    digest = hashlib.sha256()
    for item in sorted(path.rglob("*")):
        relative = item.relative_to(path)
        if "__pycache__" in relative.parts or item.suffix == ".pyc":
            continue
        if item.is_symlink():
            raise ValueError("Plugin packages cannot contain symbolic links")
        if item.is_file():
            name = relative.as_posix().encode()
            digest.update(len(name).to_bytes(8, "big"))
            digest.update(name)
            content = item.read_bytes()
            digest.update(len(content).to_bytes(8, "big"))
            digest.update(content)
    return digest.hexdigest()


@contextmanager
def use_plugins(
    names: list[str], *, on_owner_done: Callable[[], Awaitable[None]] | None = None,
) -> Iterator[None]:
    """Reserve package readers for an entire turn, including stream cleanup."""
    names = list(dict.fromkeys(_package_key(name) for name in names))
    if any(name in _writers for name in names):
        raise PluginPackageBusy("Plugin replacement is in progress; retry after it completes")
    _users.update(names)
    released = False

    def release() -> None:
        nonlocal released
        if released:
            return
        released = True
        _users.subtract(names)
        for name in names:
            if not _users[name]:
                del _users[name]

    async def close_abandoned_stream() -> None:
        # Keep readers reserved until the stream's resource cleanup finishes.
        if released:
            return
        await on_owner_done()
        release()

    owner = asyncio.current_task() if on_owner_done is not None else None
    if owner is not None:
        if owner not in _owner_streams:
            _owner_streams[owner] = []
            owner.add_done_callback(_close_owner_streams)
        _owner_streams[owner].append(close_abandoned_stream)
    try:
        yield
    finally:
        if owner is not None:
            callbacks = _owner_streams.get(owner, [])
            if close_abandoned_stream in callbacks:
                callbacks.remove(close_abandoned_stream)
                if not callbacks:
                    _owner_streams.pop(owner)
                    owner.remove_done_callback(_close_owner_streams)
        release()


@contextmanager
def replace_plugin(name: str) -> Iterator[None]:
    """Acquire an exclusive, non-waiting replacement reservation."""
    name = _package_key(name)
    if _users[name] or name in _writers:
        raise PluginPackageBusy("Plugin is executing or closing; retry when idle")
    _writers.add(name)
    try:
        yield
    finally:
        _writers.remove(name)
