# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Adapt existing Gateway container-file calls to ADX runtime primitives."""
from __future__ import annotations

from contextlib import asynccontextmanager
from contextvars import ContextVar
import mimetypes
import re
import anyio

from jiuwenswarm.extensions.adx.file_transport import file_request
import shlex
from pathlib import PurePosixPath
import uuid

from jiuwenswarm.extensions.adx.control_client import AdxControlError, AdxNotFound
from jiuwenswarm.extensions.agentos.agentos_router.router_client import (
    AgentFileDownloadChunk,
    AgentOSFileTransferError,
    normalize_agent_file_download_path,
    normalize_agent_file_upload_path,
)

_FILE_TRANSFERS: ContextVar[dict | None] = ContextVar("adx_file_transfers", default=None)
_CHUNK = 64 * 1024
_MAX_UPLOAD_SIZE = 512 * 1024 * 1024


def _validate_mode(mode: str | None) -> str | None:
    if mode is None or mode == "":
        return None
    if not isinstance(mode, str) or not re.fullmatch(r"[0-7]{3,4}", mode):
        raise AgentOSFileTransferError("invalid mode: expected 3-4 octal digits", code="BAD_REQUEST")
    return mode


class AdxFileMixin:
    native_file_streaming = True

    @staticmethod
    def _begin_file_transfer() -> None:
        # Called once at the existing file-route entry hook. Streaming tasks
        # inherit this request's pin dictionary; later requests get a new one.
        _FILE_TRANSFERS.set({})

    @asynccontextmanager
    async def _file_environment(self, user: str, agent_type: str | None, instance_id: str | None,
                                session_id: str = "", *, include_runtime: bool = False):
        runtime = None
        try:
            environment, runtime = await self._resolve_file_runtime(
                user_id=user, agent_type=agent_type, session_id=session_id, acquire=True,
            )
            if instance_id and instance_id != environment:
                raise AgentOSFileTransferError(
                    "instance does not belong to the selected user and agent", code="BAD_REQUEST",
                )
            pins = _FILE_TRANSFERS.get()
            identity = (id(self), user, agent_type or self.get_current_agent_type(user), session_id)
            if pins is not None and pins.setdefault(identity, environment) != environment:
                raise AgentOSFileTransferError("Environment changed during transfer", code="SERVICE_UNAVAILABLE")
            yield (environment, runtime) if include_runtime else environment
        except AdxControlError as exc:
            code = "instance_not_found" if isinstance(exc, AdxNotFound) else exc.code
            raise AgentOSFileTransferError(str(exc), code=code) from exc
        finally:
            if runtime is not None:
                with anyio.CancelScope(shield=True):
                    await self._agent_manager.release(runtime.key, expected_agent_id=runtime.info.agent_id)

    async def _file_invoke(self, environment: str, action: str, args: dict) -> dict:
        try:
            return await self._control.invoke(environment, action, args)
        except AdxControlError as exc:
            raise AgentOSFileTransferError(str(exc), code=exc.code) from exc

    async def upload_container_file(
        self, *, user_id: str, path: str, content: bytes, dir_prefix: str = "",
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None, mode: str | None = None,
    ) -> dict:
        async def chunks():
            for offset in range(0, len(content), _CHUNK):
                yield content[offset:offset + _CHUNK]
        return await self.upload_container_file_stream(
            user_id=user_id, path=path, chunks=chunks(), dir_prefix=dir_prefix,
            agent_type=agent_type, session_id=session_id, instance_id=instance_id, mode=mode,
        )

    async def upload_container_file_stream(
        self, *, user_id: str, path: str, chunks, dir_prefix: str = "",
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        mode: str | None = None,
    ) -> dict:
        mode = _validate_mode(mode)
        path = normalize_agent_file_upload_path(path, dir_prefix=dir_prefix, user_id=user_id)
        async with self._file_environment(user_id, agent_type, instance_id, session_id) as environment:
            return await self._upload_file_stream(environment, path, chunks, mode=mode)

    async def _upload_file_stream(self, environment: str, path: str, chunks, *, mode: str | None = None) -> dict:
        parent = PurePosixPath(path).parent
        new_directories = []
        while parent != PurePosixPath('/home/agentos'):
            info = await self._file_invoke(environment, "file.exists", {"path": str(parent)})
            if info.get("exists") is True:
                break
            new_directories.append(str(parent))
            parent = parent.parent
        temporary = str(PurePosixPath(path).parent / (".upload-" + uuid.uuid4().hex))
        size = 0
        async def counted_chunks():
            nonlocal size
            async for chunk in chunks:
                size += len(chunk)
                if size > _MAX_UPLOAD_SIZE:
                    raise AgentOSFileTransferError("upload exceeds 512 MiB", code="file_too_large")
                yield chunk
        try:
            async with file_request(self._control, environment, 'POST', temporary,
                                    content=counted_chunks()) as response:
                await response.aread()
                result = response.json()
                if result.get('error') or result.get('size') != size:
                    raise AgentOSFileTransferError("Short ADX upload", code="INTERNAL_ERROR")
            # Keep workspace ownership and publish only after a complete upload.
            ownership = await self._file_invoke(environment, "cmd.run", {
                "cmd": shlex.join(['chown', '-h', '--reference=/home/agentos', '--',
                                   temporary, *new_directories]), "timeout": 30,
            })
            if ownership.get("exit_code") != 0:
                raise AgentOSFileTransferError("Cannot apply workspace ownership", code="INTERNAL_ERROR")
            if mode is not None:
                changed = await self._file_invoke(environment, "cmd.run", {
                    "cmd": shlex.join(['chmod', '--', mode, temporary]), "timeout": 30,
                })
                if changed.get("exit_code") != 0:
                    raise AgentOSFileTransferError("Cannot apply file mode", code="INTERNAL_ERROR")
            await self._file_invoke(environment, "file.rename", {"old_path": temporary, "new_path": path})
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self._file_invoke(environment, "file.remove", {"path": temporary})
                except AgentOSFileTransferError:
                    pass
        return {"success": True, "path": path, "size": size}

    @asynccontextmanager
    async def open_container_file(
        self, *, user_id: str, path: str, agent_type: str | None = None,
        session_id: str = "", instance_id: str | None = None, byte_range: str | None = None,
    ):
        path = normalize_agent_file_download_path(path)
        async with self._file_environment(user_id, agent_type, instance_id, session_id) as environment:
            info = await self._file_invoke(environment, "file.stat", {"path": path})
            if info.get("type") != "file":
                raise AgentOSFileTransferError("path must identify a regular file", code="BAD_REQUEST")
            async with file_request(self._control, environment, 'GET', path, byte_range=byte_range) as response:
                # ADX ingress can stream without Content-Length. Preserve stat
                # metadata for HEAD, without imposing a stale length on GET.
                response.extensions['adx_file_size'] = int(info['size'])
                yield response

    async def download_container_file(
        self, *, user_id: str, path: str, offset: int = 0, limit: int = _CHUNK,
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None,
    ) -> AgentFileDownloadChunk:
        del auth_headers
        path = normalize_agent_file_download_path(path)
        offset, limit = max(0, int(offset)), min(max(1, int(limit)), _CHUNK)
        async with self._file_environment(user_id, agent_type, instance_id, session_id) as environment:
            info = await self._file_invoke(environment, "file.stat", {"path": path})
            if info.get("type") != "file":
                raise AgentOSFileTransferError("path must identify a regular file", code="BAD_REQUEST")
            size = int(info['size'])
            data = b''
            if offset < size:
                async with file_request(self._control, environment, 'GET', path,
                                        byte_range=f'bytes={offset}-{min(size, offset + limit) - 1}') as response:
                    if response.status_code != 206:
                        raise AgentOSFileTransferError("ADX did not honor the requested range")
                    async for chunk in response.aiter_bytes(chunk_size=_CHUNK):
                        data += chunk
                        if len(data) > limit:
                            raise AgentOSFileTransferError("Invalid ADX file chunk")
                if not data:
                    raise AgentOSFileTransferError("Invalid ADX file chunk")
            return AgentFileDownloadChunk(data, path, offset, len(data), size,
                mimetypes.guess_type(path)[0] or "application/octet-stream", offset + len(data) >= size)

    async def upload_attachment(self, content: bytes, target_rel_path: str, *, user_id: str,
                                session_id: str = "", channel_id: str = "web") -> dict:
        # The AgentServer resolves its injected data directory, validates the path
        # and reserves a unique name. Bytes then use the same acquired Environment.
        from jiuwenswarm.common.e2a.gateway_normalize import e2a_from_agent_fields
        from jiuwenswarm.common.schema.message import ReqMethod

        async with self._file_environment(user_id, None, None, session_id, include_runtime=True) as selected:
            environment, runtime = selected
            if runtime is None:
                raise AgentOSFileTransferError("AgentServer is unavailable", code="SERVICE_UNAVAILABLE")
            client = await self._get_ws_client(runtime)
            request = e2a_from_agent_fields(
                request_id='upload-prepare-' + uuid.uuid4().hex, channel_id=channel_id,
                session_id=session_id, user_id=user_id, req_method=ReqMethod.FILE_UPLOAD_CHUNK,
                params={'upload_id': uuid.uuid4().hex, 'target_rel_path': target_rel_path,
                        'data': '', 'prepare_only': True}, is_stream=False,
            )
            response = await client.send_request(request)
            if not response.ok:
                # Older retained AgentServers reject the empty prepare request
                # before creating a file. Only that known rejection is safe to
                # retry with their original E2A protocol.
                failure = response.payload if isinstance(response.payload, dict) else {}
                if failure.get('error') == 'data is empty' and failure.get('code') == 'BAD_REQUEST':
                    raise AgentOSFileTransferError('Retained AgentServer requires the legacy attachment protocol',
                                                   code='NATIVE_UPLOAD_PREPARE_UNSUPPORTED')
                raise AgentOSFileTransferError(str(failure.get('error') or 'Attachment path rejected'),
                                               code=str(failure.get('code') or 'BAD_REQUEST'))
            try:
                relative = str(PurePosixPath(response.payload['path']).relative_to('/home/agentos'))
            except (KeyError, TypeError, ValueError) as exc:
                raise AgentOSFileTransferError('Invalid attachment destination', code='BAD_REQUEST') from exc
            path = normalize_agent_file_upload_path(relative, user_id=user_id)
            try:
                async def chunks():
                    for offset in range(0, len(content), _CHUNK):
                        yield content[offset:offset + _CHUNK]
                return await self._upload_file_stream(environment, path, chunks())
            except BaseException:
                with anyio.CancelScope(shield=True):
                    try:
                        await self._file_invoke(environment, "file.remove", {"path": path})
                    except AgentOSFileTransferError:
                        pass
                raise

    async def list_container_files(
        self, *, user_id: str, dir_path: str, recursive: bool = False, max_depth: int = 0,
        agent_type: str | None = None, session_id: str = "", instance_id: str | None = None,
        auth_headers=None,
    ) -> list[dict]:
        del auth_headers
        path = normalize_agent_file_download_path(dir_path)
        if max_depth < 0:
            raise AgentOSFileTransferError("max_depth must be >= 0", code="BAD_REQUEST")
        async with self._file_environment(user_id, agent_type, instance_id, session_id) as environment:
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
        del auth_headers, recursive
        mode = _validate_mode(mode)
        path = normalize_agent_file_download_path(path)
        async with self._file_environment(user_id, agent_type, instance_id, session_id) as environment:
            result = await self._file_invoke(environment, "file.mkdir", {"path": path})
            if not isinstance(result.get("created"), bool):
                raise AgentOSFileTransferError("Invalid ADX mkdir response")
            if mode is not None:
                changed = await self._file_invoke(environment, "cmd.run", {
                    "cmd": shlex.join(['chmod', '--', mode, path]), "timeout": 30,
                })
                if changed.get("exit_code") != 0:
                    raise AgentOSFileTransferError("Cannot apply directory mode", code="INTERNAL_ERROR")
            return {"success": True, "path": path, "created": result["created"]}
