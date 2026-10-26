# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Adapt existing Gateway container-file calls to ADX runtime primitives."""
from __future__ import annotations

from contextvars import ContextVar
import mimetypes
from pathlib import PurePosixPath
import uuid

from jiuwenswarm.common.e2a.models import E2AEnvelope
from jiuwenswarm.extensions.adx.control_client import AdxControlError
from jiuwenswarm.extensions.adx.errors import AdxRoutingError
from gateway.routing.container_files import (
    AgentFileDownloadChunk,
    AgentOSFileTransferError,
    normalize_agent_file_download_path,
    normalize_agent_file_upload_path,
)

_FILE_TRANSFERS: ContextVar[dict | None] = ContextVar("adx_file_transfers", default=None)
_CHUNK = 64 * 1024


class AdxFileMixin:
    @staticmethod
    def _begin_file_transfer() -> None:
        # Called once at the existing file-route entry hook. Streaming tasks
        # inherit this request's pin dictionary; later requests get a new one.
        _FILE_TRANSFERS.set({})

    async def _file_environment(self, user: str, agent_type: str | None, instance_id: str | None) -> str:
        try:
            self._user(E2AEnvelope(user_id=user, channel="web", params={"agent_type": agent_type}))
            await self._get_client(user)
            environment = await self._control.binding_environment(user)
        except (AdxRoutingError, AdxControlError) as exc:
            code = {
                "ADX_USER_REQUIRED": "BAD_REQUEST",
                "ADX_USER_MISMATCH": "USER_MISMATCH",
                "ADX_AGENT_UNSUPPORTED": "BAD_REQUEST",
                "ADX_CHANNEL_UNSUPPORTED": "BAD_REQUEST",
            }.get(exc.code, exc.code)
            raise AgentOSFileTransferError(str(exc), code=code) from exc
        if instance_id and instance_id != environment:
            raise AgentOSFileTransferError("instance does not belong to the selected Binding", code="BAD_REQUEST")
        pins = _FILE_TRANSFERS.get()
        if pins is not None and pins.setdefault((id(self), user), environment) != environment:
            raise AgentOSFileTransferError("Environment changed during transfer", code="SERVICE_UNAVAILABLE")
        return environment

    async def _file_invoke(self, environment: str, action: str, args: dict) -> dict:
        try:
            return await self._control.invoke(environment, action, args)
        except AdxControlError as exc:
            raise AgentOSFileTransferError(str(exc), code=exc.code) from exc

    async def upload_container_file(
        self, *, user_id: str, path: str, content: bytes, dir_prefix: str = "",
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None,
    ) -> dict:
        del session_id, auth_headers
        path = normalize_agent_file_upload_path(path, dir_prefix=dir_prefix, user_id=user_id)
        environment = await self._file_environment(user_id, agent_type, instance_id)
        temporary = str(PurePosixPath(path).parent / (".upload-" + uuid.uuid4().hex))
        try:
            for offset in range(0, max(1, len(content)), _CHUNK):
                chunk = content[offset:offset + _CHUNK]
                result = await self._file_invoke(environment, "file.write_chunk", {
                    "path": temporary, "offset": offset, "data": chunk.hex(),
                })
                if result.get("bytes_written") != len(chunk):
                    raise AgentOSFileTransferError("Short ADX upload", code="INTERNAL_ERROR")
            await self._file_invoke(environment, "file.rename", {"old_path": temporary, "new_path": path})
        finally:
            # Unknown transport outcomes must not trigger another business write.
            # Removing the unique temporary path is safe even after atomic rename.
            try:
                await self._file_invoke(environment, "file.remove", {"path": temporary})
            except AgentOSFileTransferError:
                pass
        return {"success": True, "path": path, "size": len(content)}

    async def download_container_file(
        self, *, user_id: str, path: str, offset: int = 0, limit: int = _CHUNK,
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None,
    ) -> AgentFileDownloadChunk:
        del session_id, auth_headers
        path = normalize_agent_file_download_path(path)
        environment = await self._file_environment(user_id, agent_type, instance_id)
        offset, limit = max(0, int(offset)), min(max(1, int(limit)), _CHUNK)
        info = await self._file_invoke(environment, "file.stat", {"path": path})
        if info.get("type") != "file":
            raise AgentOSFileTransferError("path must identify a regular file", code="BAD_REQUEST")
        result = await self._file_invoke(
            environment, "file.read_chunk", {"path": path, "offset": offset, "limit": limit},
        )
        try:
            data = bytes.fromhex(result["data"])
            size = int(info["size"])
        except (ValueError, TypeError, KeyError) as exc:
            raise AgentOSFileTransferError("Invalid ADX file response") from exc
        if result.get("bytes_read") != len(data) or len(data) > limit:
            raise AgentOSFileTransferError("Invalid ADX file chunk")
        if not data and offset < size:
            raise AgentOSFileTransferError("Invalid ADX file chunk")
        return AgentFileDownloadChunk(data, path, offset, len(data), size,
            mimetypes.guess_type(path)[0] or "application/octet-stream", offset + len(data) >= size)

    async def list_container_files(
        self, *, user_id: str, dir_path: str, recursive: bool = False, max_depth: int = 0,
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None,
    ) -> list[dict]:
        del session_id, auth_headers
        path = normalize_agent_file_download_path(dir_path)
        if max_depth < 0:
            raise AgentOSFileTransferError("max_depth must be >= 0", code="BAD_REQUEST")
        environment = await self._file_environment(user_id, agent_type, instance_id)
        entries = []
        pending = [(path, max_depth)]
        while pending:
            directory, remaining = pending.pop()
            depth = min(remaining or 64, 64) if recursive else 1
            result = await self._file_invoke(environment, "file.list", {"path": directory, "depth": depth})
            batch = result.get("entries")
            if not isinstance(batch, list) or any(not isinstance(item, dict) for item in batch):
                raise AgentOSFileTransferError("Invalid ADX directory response")
            entries.extend(batch)
            if recursive and (not remaining or remaining > depth):
                for entry in batch:
                    if entry.get("type") != "dir":
                        continue
                    try:
                        relative = PurePosixPath(entry["path"]).relative_to(directory)
                    except (KeyError, TypeError, ValueError) as exc:
                        raise AgentOSFileTransferError("Invalid ADX directory entry") from exc
                    if len(relative.parts) == depth:
                        pending.append((entry["path"], remaining - depth if remaining else 0))
        entries.sort(key=lambda entry: str(entry.get("path", "")))
        return [{**entry, "is_directory": entry.get("type") == "dir",
                 "type": "directory" if entry.get("type") == "dir" else "file"} for entry in entries]

    async def mkdir_container_dir(
        self, *, user_id: str, path: str, mode: str | None = None, recursive: bool = False,
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None,
    ) -> dict:
        del session_id, auth_headers, recursive, mode
        path = normalize_agent_file_download_path(path)
        environment = await self._file_environment(user_id, agent_type, instance_id)
        result = await self._file_invoke(environment, "file.mkdir", {"path": path})
        if not isinstance(result.get("created"), bool):
            raise AgentOSFileTransferError("Invalid ADX mkdir response")
        return {"success": True, "path": path, "created": result["created"]}
