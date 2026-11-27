from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from jiuwenclaw.schema.agent import AgentResponseChunk

logger = logging.getLogger(__name__)

FILE_READ_CHUNK_EVENT_TYPE = "skilldev.file.read.chunk"
FILE_READ_CHUNK_ENCODING = "json+base64"
FILE_READ_CHUNK_RAW_BYTES = 24 * 1024
FILE_READ_UNARY_SAFE_BYTES = 48 * 1024
FILE_READ_RESPONSE_TOO_LARGE_CODE = "FILE_READ_RESPONSE_TOO_LARGE"
FILE_READ_STREAM_DECODE_MAX_ATTEMPTS = 3


class FileReadChunkDecodeError(ValueError):
    """Raised when a skilldev.file.read chunk stream cannot be reconstructed."""

    def __init__(self, message: str, *, code: str = "FILE_READ_CHUNK_DECODE_ERROR") -> None:
        super().__init__(message)
        self.code = code


def file_read_payload_to_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_file_read_payload_chunks(
    payload: dict[str, Any],
    *,
    request_id: str,
    channel_id: str,
    task_id: str,
    path: str,
    raw_chunk_bytes: int = FILE_READ_CHUNK_RAW_BYTES,
) -> Iterable[AgentResponseChunk]:
    raw = file_read_payload_to_json_bytes(payload)
    if raw_chunk_bytes <= 0:
        raise ValueError("raw_chunk_bytes must be positive")
    total = max(1, (len(raw) + raw_chunk_bytes - 1) // raw_chunk_bytes)
    for index in range(total):
        start = index * raw_chunk_bytes
        part = raw[start : start + raw_chunk_bytes]
        yield AgentResponseChunk(
            request_id=request_id,
            channel_id=channel_id,
            payload={
                "event_type": FILE_READ_CHUNK_EVENT_TYPE,
                "read_id": request_id,
                "task_id": task_id,
                "path": path,
                "encoding": FILE_READ_CHUNK_ENCODING,
                "index": index,
                "total": total,
                "data": base64.b64encode(part).decode("ascii"),
            },
            is_complete=False,
        )


def is_retriable_file_read_decode_error(exc: FileReadChunkDecodeError) -> bool:
    if exc.code != "FILE_READ_CHUNK_DECODE_ERROR":
        return False
    message = str(exc)
    return (
        "missing chunks" in message
        or "file read stream ended before any file read chunk" in message
    )


async def decode_file_read_payload_from_stream_with_retry(
    open_stream: Callable[[], AsyncIterator[AgentResponseChunk]],
    *,
    max_attempts: int = FILE_READ_STREAM_DECODE_MAX_ATTEMPTS,
    log_label: str = "skilldev.file.read",
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    last_exc: FileReadChunkDecodeError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await decode_file_read_payload_from_stream(open_stream())
        except FileReadChunkDecodeError as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_retriable_file_read_decode_error(exc):
                raise
            logger.info(
                "[%s] stream decode retry: attempt=%d/%d error=%s",
                log_label,
                attempt,
                max_attempts,
                exc,
            )
    assert last_exc is not None
    raise last_exc


async def decode_file_read_payload_from_stream(
    chunks: AsyncIterator[AgentResponseChunk],
) -> dict[str, Any]:
    parts: dict[int, str] = {}
    expected_total: int | None = None
    read_id: str | None = None
    saw_terminal = False

    async for chunk in chunks:
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        event_type = str(payload.get("event_type") or "").strip()

        if event_type == "keepalive":
            continue

        if event_type == "skilldev.error":
            message = str(payload.get("error") or "skilldev.file.read failed")
            code = str(payload.get("code") or "SKILLDEV_FILE_READ_ERROR")
            raise FileReadChunkDecodeError(message, code=code)

        if chunk.is_complete:
            if not event_type:
                saw_terminal = True
                if expected_total is not None and len(parts) == expected_total:
                    break
                continue
            raise FileReadChunkDecodeError(
                f"unexpected terminal file read chunk event_type={event_type!r}"
            )

        if event_type != FILE_READ_CHUNK_EVENT_TYPE:
            raise FileReadChunkDecodeError(
                f"unexpected file read chunk event_type={event_type!r}"
            )

        if payload.get("encoding") != FILE_READ_CHUNK_ENCODING:
            raise FileReadChunkDecodeError("unsupported file read chunk encoding")

        current_read_id = str(payload.get("read_id") or "").strip()
        if read_id is None:
            read_id = current_read_id
        elif current_read_id != read_id:
            raise FileReadChunkDecodeError("mixed read_id values in file read stream")

        try:
            index = int(payload.get("index"))
            total = int(payload.get("total"))
        except (TypeError, ValueError) as exc:
            raise FileReadChunkDecodeError("file read chunk index/total must be integers") from exc

        if total <= 0:
            raise FileReadChunkDecodeError("file read chunk total must be positive")
        if index < 0 or index >= total:
            raise FileReadChunkDecodeError("file read chunk index out of range")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise FileReadChunkDecodeError("inconsistent file read chunk total")
        if index in parts:
            raise FileReadChunkDecodeError("duplicate file read chunk index")

        data = payload.get("data")
        if not isinstance(data, str):
            raise FileReadChunkDecodeError("file read chunk data must be a base64 string")
        parts[index] = data
        if saw_terminal and expected_total is not None and len(parts) == expected_total:
            break

    if expected_total is None:
        raise FileReadChunkDecodeError("file read stream ended before any file read chunk")
    if len(parts) != expected_total:
        raise FileReadChunkDecodeError(
            f"file read stream missing chunks: expected {expected_total}, got {len(parts)}"
        )

    try:
        raw = b"".join(
            base64.b64decode(parts[index].encode("ascii"), validate=True)
            for index in range(expected_total)
        )
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise FileReadChunkDecodeError("failed to decode file read payload") from exc

    if not isinstance(decoded, dict):
        raise FileReadChunkDecodeError("file read payload must decode to an object")
    return decoded
