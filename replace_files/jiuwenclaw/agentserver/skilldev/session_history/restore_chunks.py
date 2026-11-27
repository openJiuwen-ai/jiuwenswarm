from __future__ import annotations

import base64
import json
import logging
from collections.abc import AsyncIterator, Callable, Iterable
from typing import Any

from jiuwenclaw.schema.agent import AgentResponseChunk

logger = logging.getLogger(__name__)

RESTORE_CHUNK_EVENT_TYPE = "skilldev.restore.chunk"
RESTORE_CHUNK_ENCODING = "json+base64"
RESTORE_CHUNK_RAW_BYTES = 24 * 1024
RESTORE_UNARY_SAFE_BYTES = 48 * 1024
RESTORE_RESPONSE_TOO_LARGE_CODE = "RESTORE_RESPONSE_TOO_LARGE"
RESTORE_STREAM_DECODE_MAX_ATTEMPTS = 3


class RestoreChunkDecodeError(ValueError):
    """Raised when a skilldev.restore chunk stream cannot be reconstructed."""

    def __init__(self, message: str, *, code: str = "RESTORE_CHUNK_DECODE_ERROR") -> None:
        super().__init__(message)
        self.code = code


def restore_payload_to_json_bytes(payload: dict[str, Any]) -> bytes:
    return json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def encode_restore_payload_chunks(
    payload: dict[str, Any],
    *,
    request_id: str,
    channel_id: str,
    task_id: str,
    raw_chunk_bytes: int = RESTORE_CHUNK_RAW_BYTES,
) -> Iterable[AgentResponseChunk]:
    raw = restore_payload_to_json_bytes(payload)
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
                "event_type": RESTORE_CHUNK_EVENT_TYPE,
                "restore_id": request_id,
                "task_id": task_id,
                "encoding": RESTORE_CHUNK_ENCODING,
                "index": index,
                "total": total,
                "data": base64.b64encode(part).decode("ascii"),
            },
            is_complete=False,
        )


def is_retriable_restore_decode_error(exc: RestoreChunkDecodeError) -> bool:
    """Whether a decode failure may succeed on a fresh restore stream retry."""
    if exc.code != "RESTORE_CHUNK_DECODE_ERROR":
        return False
    message = str(exc)
    return (
        "missing chunks" in message
        or "restore stream ended before any restore chunk" in message
    )


async def decode_restore_payload_from_stream_with_retry(
    open_stream: Callable[[], AsyncIterator[AgentResponseChunk]],
    *,
    max_attempts: int = RESTORE_STREAM_DECODE_MAX_ATTEMPTS,
    log_label: str = "skilldev.restore",
) -> dict[str, Any]:
    if max_attempts < 1:
        raise ValueError("max_attempts must be at least 1")

    last_exc: RestoreChunkDecodeError | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            return await decode_restore_payload_from_stream(open_stream())
        except RestoreChunkDecodeError as exc:
            last_exc = exc
            if attempt >= max_attempts or not is_retriable_restore_decode_error(exc):
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


async def decode_restore_payload_from_stream(
    chunks: AsyncIterator[AgentResponseChunk],
) -> dict[str, Any]:
    parts: dict[int, str] = {}
    expected_total: int | None = None
    restore_id: str | None = None
    saw_terminal = False

    async for chunk in chunks:
        payload = chunk.payload if isinstance(chunk.payload, dict) else {}
        event_type = str(payload.get("event_type") or "").strip()

        if event_type == "keepalive":
            continue

        if event_type == "skilldev.error":
            message = str(payload.get("error") or "skilldev.restore failed")
            code = str(payload.get("code") or "SKILLDEV_RESTORE_ERROR")
            raise RestoreChunkDecodeError(message, code=code)

        if chunk.is_complete:
            if not event_type:
                saw_terminal = True
                if expected_total is not None and len(parts) == expected_total:
                    break
                continue
            raise RestoreChunkDecodeError(
                f"unexpected terminal restore chunk event_type={event_type!r}"
            )

        if event_type != RESTORE_CHUNK_EVENT_TYPE:
            raise RestoreChunkDecodeError(
                f"unexpected restore chunk event_type={event_type!r}"
            )

        if payload.get("encoding") != RESTORE_CHUNK_ENCODING:
            raise RestoreChunkDecodeError("unsupported restore chunk encoding")

        current_restore_id = str(payload.get("restore_id") or "").strip()
        if restore_id is None:
            restore_id = current_restore_id
        elif current_restore_id != restore_id:
            raise RestoreChunkDecodeError("mixed restore_id values in restore stream")

        try:
            index = int(payload.get("index"))
            total = int(payload.get("total"))
        except (TypeError, ValueError) as exc:
            raise RestoreChunkDecodeError("restore chunk index/total must be integers") from exc

        if total <= 0:
            raise RestoreChunkDecodeError("restore chunk total must be positive")
        if index < 0 or index >= total:
            raise RestoreChunkDecodeError("restore chunk index out of range")
        if expected_total is None:
            expected_total = total
        elif total != expected_total:
            raise RestoreChunkDecodeError("inconsistent restore chunk total")
        if index in parts:
            raise RestoreChunkDecodeError("duplicate restore chunk index")

        data = payload.get("data")
        if not isinstance(data, str):
            raise RestoreChunkDecodeError("restore chunk data must be a base64 string")
        parts[index] = data
        if saw_terminal and expected_total is not None and len(parts) == expected_total:
            break

    if expected_total is None:
        raise RestoreChunkDecodeError("restore stream ended before any restore chunk")
    if len(parts) != expected_total:
        raise RestoreChunkDecodeError(
            f"restore stream missing chunks: expected {expected_total}, got {len(parts)}"
        )

    try:
        raw = b"".join(
            base64.b64decode(parts[index].encode("ascii"), validate=True)
            for index in range(expected_total)
        )
        decoded = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise RestoreChunkDecodeError("failed to decode restore payload") from exc

    if not isinstance(decoded, dict):
        raise RestoreChunkDecodeError("restore payload must decode to an object")
    return decoded
