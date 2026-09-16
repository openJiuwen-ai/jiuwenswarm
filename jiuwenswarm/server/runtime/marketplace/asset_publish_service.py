# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Internal orchestration for one workspace's publishing operations.

The transport layer must authenticate users and derive scope from workspace,
publisher and Hub. Neither scope nor local source paths are client assertions.
The resolver must authorize local IDs in that scope before returning a directory.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import replace
import hashlib
import logging
import os
from pathlib import Path
import shutil
import stat
import time
import tempfile

from .asset_package_builder import build_asset_package
from .asset_publish_models import PublishIdentity
from .asset_publish_store import PublishStore, PublishStoreError
from .asset_mcp_publish_converter import CustomMcpSource, convert_mcp_config
from .hub_publish_client import PublishAuth, PublishRequest, PublishUploadError
from .hub_publish_port import HubPublishPort


class AssetPublishService:
    def __init__(
        self,
        store: PublishStore,
        publisher: HubPublishPort,
        resolve_source: Callable[[str, str, str], Path | CustomMcpSource],
        artifact_root: Path,
        *,
        draft_ttl: float = 1800,
        max_drafts: int = 64,
        max_artifact_bytes: int = 512 * 1024 * 1024,
        cleanup_interval: float = 60,
        on_published: Callable | None = None,
    ):
        if draft_ttl <= 0:
            raise ValueError("draft_ttl must be positive")
        if max_drafts < 1 or max_artifact_bytes < 1 or cleanup_interval <= 0:
            raise ValueError("invalid_publish_limits")
        self._max_drafts = max_drafts
        self._max_artifact_bytes = max_artifact_bytes
        self._cleanup_interval = cleanup_interval
        self._on_published = on_published
        self._started = False
        self._maintenance_task = None
        self._prepare_lock = asyncio.Lock()
        self._prepares = set()
        self._building = False
        self._store = store
        self._publisher = publisher
        self._resolve_source = resolve_source
        self._artifact_root = Path(artifact_root)
        self._draft_ttl = draft_ttl
        self._tasks: dict[asyncio.Task, tuple[str, str]] = {}
        self._upload_slot = asyncio.Semaphore(1)
        self._closed = False

    async def start(self) -> None:
        """Recover once before accepting work; never replay an upload after restart."""
        if self._closed:
            raise RuntimeError("publisher_closed")
        if self._started:
            return
        self._store.recover_interrupted()
        self.cleanup()
        self._started = True
        self._maintenance_task = asyncio.create_task(self._maintain())

    async def _maintain(self) -> None:
        while True:
            await asyncio.sleep(self._cleanup_interval)
            # Building is serialized; do not collect its not-yet-recorded output.
            async with self._prepare_lock:
                self.cleanup()

    def cleanup(self, *, now: float | None = None) -> None:
        if self._building:
            return
        keep, remove = self._store.cleanup_artifacts(now=now)
        for path in remove:
            self._remove_artifact(path)
        live = {path.parent for path in keep}
        if self._artifact_root.is_dir():
            for child in self._artifact_root.iterdir():
                if (
                    child.name.startswith(("publish-", "mcp-source-"))
                    and child not in live
                ):
                    if child.is_symlink():
                        child.unlink()
                    elif child.is_dir():
                        shutil.rmtree(child, ignore_errors=True)

    def _artifact_bytes(self) -> int:
        return sum(
            path.stat().st_size
            for path in self._artifact_root.rglob("*")
            if path.is_file() and not path.is_symlink()
        )

    def _build(self, source, identity, metadata):
        if isinstance(source, CustomMcpSource):
            self._artifact_root.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(
                prefix="mcp-source-", dir=self._artifact_root
            ) as directory:
                root = convert_mcp_config(
                    source.config, identity, metadata, Path(directory)
                )
                package = build_asset_package(
                    root, identity, metadata, self._artifact_root
                )
                return replace(
                    package,
                    normalizations=package.normalizations
                    + (
                        "manifest.display_name: generated zh/en from confirmed display name",
                        "manifest.display_description: generated zh/en from confirmed description",
                        "mcp.json: credential values replaced with declared placeholders where present",
                        "mcp.json: remote endpoint, when present, replaced with MCP_URL for the installer to supply",
                        "README.md: generated installation instructions",
                    ),
                )
        return build_asset_package(source, identity, metadata, self._artifact_root)

    async def prepare(
        self, scope: str, local_id: str, identity: PublishIdentity, metadata: dict
    ) -> dict:
        await self.start()
        task = asyncio.current_task()
        self._prepares.add(task)
        try:
            async with self._prepare_lock:
                if self._closed:
                    raise RuntimeError("publisher_closed")
                self.cleanup()
                if self._store.pending_draft_count() >= self._max_drafts:
                    raise PublishStoreError("draft_limit")
                if self._artifact_bytes() >= self._max_artifact_bytes:
                    raise PublishStoreError("artifact_limit")
                self._building = True
                try:
                    return await self._prepare(scope, local_id, identity, metadata)
                finally:
                    self._building = False
        finally:
            self._prepares.discard(task)

    async def _prepare(
        self, scope: str, local_id: str, identity: PublishIdentity, metadata: dict
    ) -> dict:
        if self._closed:
            raise RuntimeError("publisher_closed")
        # Run only an authorized resolver, never accept a browser-provided path.
        source = self._resolve_source(scope, identity.kind, local_id)
        build_task = asyncio.create_task(
            asyncio.to_thread(
                self._build,
                source,
                identity,
                metadata,
            )
        )
        try:
            package = await asyncio.shield(build_task)
        except asyncio.CancelledError:
            # A worker thread cannot be cancelled; collect and delete its eventual output.
            while not build_task.done():
                try:
                    await asyncio.shield(build_task)
                except asyncio.CancelledError:
                    continue
                except Exception:
                    break
            try:
                package = build_task.result()
                self._remove_artifact(package.artifact_path)
            except Exception:
                logging.getLogger(__name__).debug("Cancelled package cleanup failed", exc_info=True)
            raise
        try:
            if self._closed:
                raise RuntimeError("publisher_closed")
            if self._artifact_bytes() > self._max_artifact_bytes:
                raise PublishStoreError("artifact_limit")
            request = PublishRequest(
                identity=identity,
                artifact_path=package.artifact_path,
                artifact_sha256=package.artifact_sha256,
                display_name=metadata.get("display_name", ""),
                description=metadata.get("description", ""),
                tags=tuple(metadata.get("tags", ())),
                version_desc=metadata.get("version_desc", ""),
                visibility=metadata.get("visibility", "public"),
                force=metadata.get("force", False),
            )
            expires_at = time.time() + self._draft_ttl
            draft_id = self._store.save_draft(
                scope, local_id, request, expires_at=expires_at
            )
        except BaseException:
            self._remove_artifact(package.artifact_path)
            raise
        return dict(
            draft_id=draft_id,
            expires_at=expires_at,
            kind=identity.kind,
            package_name=identity.package_name,
            version=identity.version,
            target_asset_id=identity.target_asset_id,
            checksum_sha256=package.artifact_sha256,
            size_bytes=package.size_bytes,
            files=list(package.files),
            excluded=list(package.excluded),
            normalizations=list(package.normalizations),
            dependencies=list(package.dependencies),
        )

    async def commit(
        self, scope: str, draft_id: str, request_id: str, *, auth: PublishAuth
    ) -> dict:
        if self._closed:
            raise RuntimeError("publisher_closed")
        await self.start()
        if not isinstance(auth, PublishAuth):
            raise ValueError("publish_auth_required")
        operation_id, created = self._store.commit_draft(scope, draft_id, request_id)
        if created:
            # Credentials exist only in this task's memory, never in SQLite or status.
            task = asyncio.create_task(self._run(scope, operation_id, auth))
            self._tasks[task] = (scope, operation_id)
            task.add_done_callback(self._task_done)
        return self.status(scope, operation_id)

    def _task_done(self, task: asyncio.Task) -> None:
        scope, operation_id = self._tasks.pop(task)
        if not task.cancelled():
            task.exception()  # Retrieve unexpected internal failures without logging secrets.
        # Cancellation can happen before the coroutine enters its try/finally.
        if (
            task.cancelled()
            and self.status(scope, operation_id)["execution_status"] == "queued"
        ):
            self._store.finish(
                scope,
                operation_id,
                execution_status="failed",
                error={"code": "interrupted", "outcome_unknown": False},
            )
            self._remove_artifact(
                self._store.operation_request(scope, operation_id).artifact_path
            )

    async def _run(self, scope: str, operation_id: str, auth: PublishAuth) -> None:
        request = None
        started = False
        try:
            request = self._store.operation_request(scope, operation_id)
            async with self._upload_slot:
                if not self._store.start(scope, operation_id):
                    return
                await asyncio.to_thread(self._verify_artifact, request)
                started = True
                result = await self._publisher.publish(request, auth=auth)
                self._store.finish(
                    scope,
                    operation_id,
                    execution_status="completed",
                    result={
                        "asset_id": result.asset_id,
                        "kind": result.identity.kind,
                        "package_name": result.identity.package_name,
                        "version": result.identity.version,
                        "publish_result": result.publish_result,
                        "visibility": result.visibility,
                        "deduplicated": result.deduplicated,
                    },
                )
                if (
                    self._on_published is not None
                    and result.visibility == "public"
                    and result.publish_result in {"publish_success", "published"}
                ):
                    try:
                        self._on_published(request, result)
                    except Exception:
                        pass  # Durable Hub outcome must survive local cache failures.
        except PublishUploadError as exc:
            self._store.finish(
                scope,
                operation_id,
                execution_status="unknown" if exc.outcome_unknown else "failed",
                error={
                    "code": exc.code,
                    "http_status": exc.http_status,
                    "outcome_unknown": exc.outcome_unknown,
                    "retry_after": exc.retry_after,
                },
            )
        except asyncio.CancelledError:
            self._store.finish(
                scope,
                operation_id,
                execution_status="unknown" if started else "failed",
                error={"code": "interrupted", "outcome_unknown": started},
            )
            raise
        except Exception:
            # An unexpected failure after entering the upload stage cannot prove rejection.
            self._store.finish(
                scope,
                operation_id,
                execution_status="unknown" if started else "failed",
                error={"code": "publish_failed", "outcome_unknown": started},
            )
        finally:
            if request is not None and self.status(scope, operation_id)[
                "execution_status"
            ] in {"completed", "failed", "unknown"}:
                self._remove_artifact(request.artifact_path)

    @staticmethod
    def _verify_artifact(request: PublishRequest) -> None:
        try:
            before = request.artifact_path.lstat()
            fd = os.open(
                request.artifact_path,
                os.O_RDONLY
                | getattr(os, "O_NOFOLLOW", 0)
                | getattr(os, "O_NONBLOCK", 0),
                0o600,
            )
            with os.fdopen(fd, "rb") as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or (before.st_dev, before.st_ino) != (
                    info.st_dev,
                    info.st_ino,
                ):
                    raise PublishUploadError("artifact_changed")
                digest = hashlib.sha256()
                count = 0
                while True:
                    chunk = stream.read(65536)
                    if not chunk:
                        break
                    count += len(chunk)
                    if count > 50 * 1024 * 1024:
                        raise PublishUploadError("file_too_large")
                    digest.update(chunk)
            if digest.hexdigest() != request.artifact_sha256:
                raise PublishUploadError("artifact_changed")
        except OSError:
            raise PublishUploadError("artifact_unavailable") from None

    def _remove_artifact(self, path: Path) -> None:
        # Delete only builder-owned staging directories, never arbitrary persisted paths.
        parent = path.parent
        if (
            parent.parent.resolve() == self._artifact_root.resolve()
            and parent.name.startswith("publish-")
        ):
            shutil.rmtree(parent, ignore_errors=True)

    def status(self, scope: str, operation_id: str) -> dict:
        return self._store.status(scope, operation_id)

    def records(self, scope: str, kind: str, local_id: str) -> list[dict]:
        return self._store.records(scope, kind, local_id)

    async def wait_idle(self) -> None:
        while self._tasks:
            await asyncio.gather(*tuple(self._tasks), return_exceptions=True)

    async def close(self) -> None:
        self._closed = True
        for task in tuple(self._tasks):
            task.cancel()
        if self._maintenance_task is not None:
            self._maintenance_task.cancel()
            await asyncio.gather(self._maintenance_task, return_exceptions=True)
        prepares = tuple(self._prepares)
        for task in prepares:
            task.cancel()
        if prepares:
            await asyncio.gather(*prepares, return_exceptions=True)
        for task in tuple(self._tasks):
            task.cancel()
        await self.wait_idle()
        self.cleanup()
