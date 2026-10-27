# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Worker-side host of the trajectory read API.

The Gateway keeps origin checks, query-integer checks, and the session mode
gate, then forwards the read. This module owns the SQLite reader and the
``trajectory_ui`` settings, and returns either a JSON body or a chunk handle
the Gateway drains before it starts the HTTP response.
"""

from __future__ import annotations

import asyncio
import base64
import io
import json
import logging
import re
import tempfile
import uuid
import zipfile
from collections.abc import AsyncGenerator
from contextlib import aclosing
from pathlib import Path
from typing import IO, Any

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponse
from jiuwenswarm.common.schema.message import ReqMethod

logger = logging.getLogger(__name__)

TRAJECTORY_ARCHIVE_ENTRY_NAME = "trajectory.jsonl"
_CHUNK_BYTES = 2 * 1024 * 1024
_ARCHIVE_SPOOL_MAX_BYTES = 16 * 1024 * 1024
_ARCHIVE_WRITE_CHUNK_BYTES = 1024 * 1024
_MAX_HELD_BLOBS = 32
_MAX_SQLITE_INTEGER = (1 << 63) - 1
_MAX_FRAME_PAGE = 2000
_MAX_SEQUENCE_REQUEST = 200
_TRACE_ID_PATTERN = re.compile(r"^[0-9a-f]{32}$")
_SPAN_ID_PATTERN = re.compile(r"^[0-9a-f]{16}$")
_SEQUENCE_HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")

_JSON_METHODS = frozenset(
    {
        ReqMethod.TRAJECTORY_SUBJECTS_LIST,
        ReqMethod.TRAJECTORY_STREAM_FRAMES_GET,
        ReqMethod.TRAJECTORY_SEQUENCES_GET,
        ReqMethod.TRAJECTORY_CHECKPOINTS_GET,
        ReqMethod.TRAJECTORY_USAGE_GET,
        ReqMethod.TRAJECTORY_SUBJECT_GET,
        ReqMethod.TRAJECTORY_RAW_GET,
        ReqMethod.TRAJECTORY_ARCHIVE_OPEN,
    }
)


class TrajectoryQueryError(Exception):
    """A trajectory read the caller should see as an HTTP error."""

    def __init__(self, message: str, code: str, status: int) -> None:
        super().__init__(message)
        self.message = message
        self.code = code
        self.status = status


class _HeldBlob:
    """Bytes or a spooled file the Gateway reads in 2 MiB slices."""

    def __init__(self, data: bytes | IO[bytes]) -> None:
        self._data = data
        if isinstance(data, bytes):
            self.size = len(data)
            return
        data.seek(0, io.SEEK_END)
        self.size = data.tell()
        data.seek(0)

    def read(self, offset: int, count: int) -> bytes:
        if isinstance(self._data, bytes):
            return self._data[offset:offset + count]
        self._data.seek(offset)
        return self._data.read(count)

    def close(self) -> None:
        if not isinstance(self._data, bytes):
            self._data.close()


class TrajectoryHost:
    """Answer ``trajectory.*`` reads against the local trajectory store."""

    def __init__(
        self,
        settings: Any = None,
        *,
        reader: Any = None,
    ) -> None:
        """Bind an optional pinned settings snapshot and reader.

        Args:
            settings: Pinned ``TrajectoryStoreSettings``. Omit it so each call
                reads the live ``trajectory_ui`` block.
            reader: Reader override. Production builds one from the settings.
        """
        self._pinned_settings = settings
        self._reader_override = reader
        self._reader: Any = None
        self._reader_database_path: Path | None = None
        self._blobs: dict[str, _HeldBlob] = {}

    @property
    def settings(self) -> Any:
        """Return the pinned snapshot, or the live ``trajectory_ui`` settings."""
        if self._pinned_settings is not None:
            return self._pinned_settings
        from jiuwenswarm.observability.config import load_trajectory_store_settings

        return load_trajectory_store_settings()

    def reader_for(self, settings: Any) -> Any:
        """Return the reader bound to ``settings.database_path``."""
        if self._reader_override is not None:
            return self._reader_override
        from jiuwenswarm.observability.store import AsyncTrajectoryReader

        database_path = settings.database_path
        reader = self._reader
        if reader is None or self._reader_database_path != database_path:
            reader = AsyncTrajectoryReader(database_path, session_scoped=True)
            self._reader = reader
            self._reader_database_path = database_path
        return reader

    async def call(
        self,
        method: ReqMethod,
        params: dict[str, Any],
        user_id: str | None = None,
    ) -> tuple[bool, dict[str, Any]]:
        """Run one trajectory read and return ``(ok, payload)``.

        ``user_id`` is accepted so this matches the Gateway fetch signature.
        The store is local and is not partitioned by that id.
        """
        del user_id
        try:
            if method == ReqMethod.TRAJECTORY_BLOB_READ:
                payload = self._read_blob(params)
            elif method == ReqMethod.TRAJECTORY_BLOB_CLOSE:
                payload = self._close_blob(params)
            elif method in _JSON_METHODS:
                current = self.settings
                if not current.enabled:
                    raise TrajectoryQueryError(
                        "trajectory UI is disabled",
                        "TRAJECTORY_DISABLED",
                        503,
                    )
                payload = await self._query(method, params, current)
            else:
                raise TrajectoryQueryError(
                    f"unsupported trajectory method: {method}",
                    "BAD_REQUEST",
                    400,
                )
        except TrajectoryQueryError as exc:
            return False, {"error": exc.message, "code": exc.code, "status": exc.status}
        except Exception:
            logger.exception("[trajectory] query failed: method=%s", method)
            return False, {
                "error": "trajectory query failed",
                "code": "TRAJECTORY_QUERY_FAILED",
                "status": 500,
            }
        return True, payload

    async def handle(self, request: AgentRequest) -> AgentResponse:
        """Answer one ``trajectory.*`` request from the Gateway."""
        params = request.params if isinstance(request.params, dict) else {}
        method = request.req_method if isinstance(request.req_method, ReqMethod) else None
        if method is None:
            return self._error(request, "unsupported trajectory method", "BAD_REQUEST", 400)
        ok, payload = await self.call(method, dict(params))
        if not ok:
            return self._error(
                request,
                str(payload.get("error") or "trajectory query failed"),
                str(payload.get("code") or "TRAJECTORY_QUERY_FAILED"),
                int(payload.get("status") or 500),
            )
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=True,
            payload=payload,
        )

    @staticmethod
    def _error(request: AgentRequest, message: str, code: str, status: int) -> AgentResponse:
        return AgentResponse(
            request_id=request.request_id,
            channel_id=request.channel_id,
            ok=False,
            payload={"error": message, "code": code, "status": status},
        )

    async def _query(
        self,
        method: ReqMethod,
        params: dict[str, Any],
        settings: Any,
    ) -> dict[str, Any]:
        session_id = str(params.get("session_id") or "")
        reader = self.reader_for(settings)
        if method == ReqMethod.TRAJECTORY_SUBJECTS_LIST:
            return await self._list_subjects(reader, session_id, params)
        if method == ReqMethod.TRAJECTORY_STREAM_FRAMES_GET:
            return await self._stream_frames(reader, session_id, params)
        if method == ReqMethod.TRAJECTORY_SEQUENCES_GET:
            return await self._sequences(reader, session_id, params)
        if method == ReqMethod.TRAJECTORY_CHECKPOINTS_GET:
            return await self._checkpoints(reader, session_id)
        if method == ReqMethod.TRAJECTORY_USAGE_GET:
            return await self._usage(reader, session_id)
        if method == ReqMethod.TRAJECTORY_SUBJECT_GET:
            return await self._subject(reader, session_id, params, settings)
        if method == ReqMethod.TRAJECTORY_RAW_GET:
            return await self._raw(reader, session_id, params)
        if method == ReqMethod.TRAJECTORY_ARCHIVE_OPEN:
            return await self._archive(reader, session_id)
        raise TrajectoryQueryError(
            f"unsupported trajectory method: {method}",
            "BAD_REQUEST",
            400,
        )

    async def _list_subjects(self, reader: Any, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        after_revision = _required_int(params.get("after_revision", 0), "after_revision")
        items, store_epoch, watermark = await reader.list_subjects(
            session_id,
            after_revision=after_revision,
        )
        return self._json_body({
            "schema_version": 1,
            "session_id": session_id,
            "items": items,
            "watermark": watermark,
            "store_epoch": store_epoch,
        })

    async def _stream_frames(self, reader: Any, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        since_frame_seq = _required_int(params.get("since_frame_seq", 0), "since_frame_seq")
        limit = _bounded_int(params.get("limit", 500), "limit", 1, _MAX_FRAME_PAGE)
        result = await reader.get_stream_frames(
            session_id,
            since_frame_seq=since_frame_seq,
            limit=limit,
        )
        if result is None:
            raise TrajectoryQueryError("session not found", "NOT_FOUND", 404)
        return self._json_body({"schema_version": 1, "session_id": session_id, **result})

    async def _sequences(self, reader: Any, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        raw_hashes = params.get("hashes") or []
        if isinstance(raw_hashes, str):
            requested = [value for value in raw_hashes.split(",") if value]
        else:
            requested = [str(value or "").strip() for value in raw_hashes]
            requested = [value for value in requested if value]
        if not requested:
            raise TrajectoryQueryError("at least one sequence hash is required", "BAD_REQUEST", 400)
        if len(requested) > _MAX_SEQUENCE_REQUEST:
            raise TrajectoryQueryError(
                f"at most {_MAX_SEQUENCE_REQUEST} sequences per request",
                "BAD_REQUEST",
                400,
            )
        if any(_SEQUENCE_HASH_PATTERN.fullmatch(value) is None for value in requested):
            raise TrajectoryQueryError("invalid sequence hash", "BAD_REQUEST", 400)
        since_revision = _required_int(params.get("since_revision", 0), "since_revision")
        resolved = await reader.resolve_sequences(
            session_id,
            requested,
            since_revision=max(0, since_revision),
        )
        if resolved is None:
            raise TrajectoryQueryError("session not found", "NOT_FOUND", 404)
        return self._json_body({"schema_version": 1, "session_id": session_id, **resolved})

    async def _checkpoints(self, reader: Any, session_id: str) -> dict[str, Any]:
        result = await reader.get_retention_checkpoints(session_id)
        if result is None:
            result = {
                "store_epoch": "absent",
                "checkpoints": [],
                "sequences": {},
                "blobs": {},
            }
        return self._json_body({"schema_version": 1, "session_id": session_id, **result})

    async def _usage(self, reader: Any, session_id: str) -> dict[str, Any]:
        items, store_epoch = await reader.get_session_request_usage(session_id)
        return self._json_body({
            "schema_version": 1,
            "session_id": session_id,
            "store_epoch": store_epoch,
            "scope": "session",
            "items": items,
        })

    async def _subject(
        self,
        reader: Any,
        session_id: str,
        params: dict[str, Any],
        settings: Any,
    ) -> dict[str, Any]:
        subject_id = str(params.get("subject_id") or "").strip()
        if not subject_id or len(subject_id) > 256:
            raise TrajectoryQueryError("invalid subject_id", "BAD_REQUEST", 400)
        since_revision = _required_int(params.get("since_revision", 0), "since_revision")
        limit = _bounded_int(params.get("limit", 1000), "limit", 1, 1000)
        result = await reader.get_subject_records(
            session_id,
            subject_id,
            since_revision=since_revision,
            limit=limit,
            max_bytes=settings.detail_max_bytes,
        )
        if result is None:
            raise TrajectoryQueryError("subject not found", "NOT_FOUND", 404)
        head_hashes: set[str] = set()
        for record in result.get("records", ()):
            for reference in (record.get("sequences") or {}).values():
                if reference.get("hash"):
                    head_hashes.add(str(reference["hash"]))
        resolved: dict[str, Any] = {"sequences": {}, "blobs": {}}
        if head_hashes:
            found = await reader.resolve_sequences(
                session_id,
                sorted(head_hashes),
                since_revision=since_revision,
            )
            if found is not None:
                resolved = found
        return self._json_body({
            "schema_version": 1,
            "session_id": session_id,
            "subject_id": subject_id,
            **result,
            **resolved,
        })

    async def _raw(self, reader: Any, session_id: str, params: dict[str, Any]) -> dict[str, Any]:
        trace_id = str(params.get("trace_id") or "").strip().lower()
        span_id = str(params.get("span_id") or "").strip().lower()
        if _TRACE_ID_PATTERN.fullmatch(trace_id) is None:
            raise TrajectoryQueryError("invalid trace_id", "BAD_REQUEST", 400)
        if _SPAN_ID_PATTERN.fullmatch(span_id) is None:
            raise TrajectoryQueryError("invalid span_id", "BAD_REQUEST", 400)
        raw_json = await reader.get_raw_record(session_id, trace_id, span_id)
        if raw_json is None:
            raise TrajectoryQueryError("span not found", "NOT_FOUND", 404)
        raw_bytes = raw_json if isinstance(raw_json, bytes) else bytes(raw_json)
        if len(raw_bytes) > _CHUNK_BYTES:
            return self._hold(raw_bytes, "application/json")
        return {"raw_b64": base64.b64encode(raw_bytes).decode("ascii")}

    async def _archive(self, reader: Any, session_id: str) -> dict[str, Any]:
        spool = tempfile.SpooledTemporaryFile(max_size=_ARCHIVE_SPOOL_MAX_BYTES)
        try:
            await _write_archive_zip(reader.iter_session_archive_lines(session_id), spool)
        except Exception:
            spool.close()
            raise
        return self._hold(spool, "application/zip")

    def _json_body(self, result: dict[str, Any]) -> dict[str, Any]:
        encoded = json.dumps(result).encode("utf-8")
        if len(encoded) > _CHUNK_BYTES:
            return self._hold(encoded, "application/json")
        return {"result": result}

    def _hold(self, data: bytes | IO[bytes], content_type: str) -> dict[str, Any]:
        while len(self._blobs) >= _MAX_HELD_BLOBS:
            oldest = next(iter(self._blobs))
            self._blobs.pop(oldest).close()
        export_id = uuid.uuid4().hex
        blob = _HeldBlob(data)
        self._blobs[export_id] = blob
        return {
            "chunked": True,
            "export_id": export_id,
            "size": blob.size,
            "content_type": content_type,
        }

    def _read_blob(self, params: dict[str, Any]) -> dict[str, Any]:
        export_id = str(params.get("export_id") or "")
        blob = self._blobs.get(export_id)
        if blob is None:
            raise TrajectoryQueryError("trajectory export not found", "NOT_FOUND", 404)
        offset = _required_int(params.get("offset", 0), "offset")
        chunk = blob.read(offset, _CHUNK_BYTES) if offset < blob.size else b""
        next_offset = offset + len(chunk)
        return {
            "data": base64.b64encode(chunk).decode("ascii"),
            "next_offset": next_offset,
            "done": next_offset >= blob.size,
        }

    def _close_blob(self, params: dict[str, Any]) -> dict[str, Any]:
        export_id = str(params.get("export_id") or "")
        blob = self._blobs.pop(export_id, None)
        if blob is not None:
            blob.close()
        return {}


def _required_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        try:
            if isinstance(value, str) and value.isdecimal():
                value = int(value)
            else:
                raise TypeError
        except (TypeError, ValueError, OverflowError) as exc:
            raise TrajectoryQueryError(f"{name} must be an integer", "BAD_REQUEST", 400) from exc
    if value < 0:
        raise TrajectoryQueryError(f"{name} must be >= 0", "BAD_REQUEST", 400)
    if value > _MAX_SQLITE_INTEGER:
        raise TrajectoryQueryError(f"{name} is too large", "BAD_REQUEST", 400)
    return value


def _bounded_int(value: Any, name: str, low: int, high: int) -> int:
    parsed = _required_int(value, name)
    if not low <= parsed <= high:
        raise TrajectoryQueryError(
            f"{name} must be between {low} and {high}",
            "BAD_REQUEST",
            400,
        )
    return parsed


async def _write_archive_zip(
    lines: AsyncGenerator[dict[str, Any], None],
    target: IO[bytes],
) -> None:
    """Deflate archive lines into a one-entry zip written to *target*."""
    async with aclosing(lines) as stream:
        with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            with archive.open(TRAJECTORY_ARCHIVE_ENTRY_NAME, "w", force_zip64=True) as entry:
                pending = bytearray()
                async for line in stream:
                    pending += json.dumps(
                        line,
                        ensure_ascii=False,
                        separators=(",", ":"),
                    ).encode("utf-8")
                    pending += b"\n"
                    if len(pending) >= _ARCHIVE_WRITE_CHUNK_BYTES:
                        await asyncio.to_thread(entry.write, bytes(pending))
                        pending.clear()
                if pending:
                    await asyncio.to_thread(entry.write, bytes(pending))
