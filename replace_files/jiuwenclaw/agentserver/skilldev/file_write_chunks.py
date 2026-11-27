from __future__ import annotations

import asyncio
import base64
import binascii
import hashlib
import json
import re
import shutil
import time
from collections.abc import Awaitable, Callable, Iterable
from pathlib import Path
from typing import Any

from jiuwenclaw.schema.agent import AgentResponse

FILE_WRITE_CHUNK_RAW_BYTES = 24 * 1024
FILE_WRITE_UNARY_SAFE_BYTES = 48 * 1024
FILE_WRITE_MAX_RETRIES = 2
FILE_WRITE_STAGING_TTL_SECONDS = 60 * 60
FILE_WRITE_MAX_STAGING_BYTES = 4 * 1_048_576
FILE_WRITE_CHUNK_ENCODING = "base64"

_WRITE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_RETRIABLE_CODES = {
    "BAD_GATEWAY",
    "GATEWAY_TIMEOUT",
    "INTERNAL_ERROR",
    "OA_BAD_GATEWAY",
    "OA_GATEWAY_TIMEOUT",
    "OA_SERVICE_UNAVAILABLE",
    "SERVICE_UNAVAILABLE",
    "TIMEOUT",
}
_INTERNAL_FILE_WRITE_FIELDS = {
    "data",
    "encoding",
    "index",
    "phase",
    "sha256",
    "size_bytes",
    "total",
    "write_id",
}


class FileWriteChunkError(ValueError):
    pass


class FileWriteTransportError(RuntimeError):
    pass


def file_write_params_to_json_bytes(params: dict[str, Any]) -> bytes:
    return json.dumps(params, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def should_chunk_file_write(params: dict[str, Any], content: str) -> bool:
    return len(file_write_params_to_json_bytes({**params, "content": content})) > FILE_WRITE_UNARY_SAFE_BYTES


def encode_file_write_chunks(
    content: str,
    *,
    write_id: str,
    raw_chunk_bytes: int = FILE_WRITE_CHUNK_RAW_BYTES,
) -> Iterable[dict[str, Any]]:
    if raw_chunk_bytes <= 0:
        raise ValueError("raw_chunk_bytes must be positive")
    raw = content.encode("utf-8")
    digest = hashlib.sha256(raw).hexdigest()
    total = max(1, (len(raw) + raw_chunk_bytes - 1) // raw_chunk_bytes)
    for index in range(total):
        part = raw[index * raw_chunk_bytes : (index + 1) * raw_chunk_bytes]
        yield {
            "phase": "chunk",
            "write_id": write_id,
            "encoding": FILE_WRITE_CHUNK_ENCODING,
            "index": index,
            "total": total,
            "size_bytes": len(raw),
            "sha256": digest,
            "data": base64.b64encode(part).decode("ascii"),
        }


def _response_payload(response: AgentResponse | Any) -> dict[str, Any]:
    payload = getattr(response, "payload", None)
    return dict(payload) if isinstance(payload, dict) else {}


def _response_succeeded(response: AgentResponse | Any) -> bool:
    return bool(getattr(response, "ok", False)) and _response_payload(response).get("ok", True) is not False


def _response_is_retriable(response: AgentResponse | Any) -> bool:
    if getattr(response, "ok", False):
        return False
    payload = _response_payload(response)
    for key in ("status", "status_code", "http_status"):
        try:
            if int(payload.get(key)) >= 500:
                return True
        except (TypeError, ValueError):
            pass
    return str(payload.get("code") or "").strip().upper() in _RETRIABLE_CODES


async def _send_with_retry(
    send_request: Callable[[dict[str, Any], str, int], Awaitable[AgentResponse]],
    params: dict[str, Any],
    phase: str,
    *,
    max_retries: int,
) -> AgentResponse:
    last_exc: Exception | None = None
    last_response: AgentResponse | None = None
    for attempt in range(max_retries + 1):
        try:
            response = await send_request(params, phase, attempt)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # transport errors are retriable
            last_exc = exc
        else:
            last_response = response
            if not _response_is_retriable(response):
                return response
        if attempt < max_retries:
            await asyncio.sleep(0.1 * (2**attempt))
    if last_response is not None:
        return last_response
    raise FileWriteTransportError(f"skilldev.file.write {phase} failed after retries: {last_exc}") from last_exc


async def send_file_write_with_chunks(
    *,
    base_params: dict[str, Any],
    content: str,
    write_id: str,
    send_request: Callable[[dict[str, Any], str, int], Awaitable[AgentResponse]],
    max_retries: int = FILE_WRITE_MAX_RETRIES,
) -> AgentResponse:
    """Send a file write, transparently switching to the internal chunk protocol."""
    if max_retries < 0:
        raise ValueError("max_retries must be non-negative")
    try:
        content_bytes = content.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise FileWriteChunkError("file write content is not valid UTF-8") from exc
    base_params = {
        key: value
        for key, value in base_params.items()
        if key not in _INTERNAL_FILE_WRITE_FIELDS and key != "content"
    }
    if not should_chunk_file_write(base_params, content):
        return await send_request({**base_params, "content": content}, "legacy", 0)

    chunks = list(encode_file_write_chunks(content, write_id=write_id))
    metadata = {
        "write_id": write_id,
        "total": len(chunks),
        "size_bytes": len(content_bytes),
        "sha256": hashlib.sha256(content_bytes).hexdigest(),
    }

    async def abort() -> None:
        try:
            await send_request({**base_params, **metadata, "phase": "abort"}, "abort", 0)
        except Exception:
            pass

    async def resolve_status(commit_error: Exception | None = None) -> AgentResponse:
        try:
            return await send_request(
                {**base_params, **metadata, "phase": "status"}, "status", 0
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            await abort()
            message = "skilldev.file.write status failed after commit retries"
            raise FileWriteTransportError(f"{message}: {exc}") from (commit_error or exc)

    for chunk in chunks:
        try:
            response = await _send_with_retry(
                send_request,
                {**base_params, **chunk},
                "chunk",
                max_retries=max_retries,
            )
        except FileWriteTransportError:
            await abort()
            raise
        if not _response_succeeded(response):
            await abort()
            return response

    commit_params = {**base_params, **metadata, "phase": "commit"}
    try:
        response = await _send_with_retry(
            send_request,
            commit_params,
            "commit",
            max_retries=max_retries,
        )
    except FileWriteTransportError as commit_exc:
        status = await resolve_status(commit_exc)
        status_payload = _response_payload(status)
        if _response_succeeded(status) and status_payload.get("status") == "committed":
            return status
        await abort()
        raise commit_exc

    if _response_succeeded(response):
        return response
    if _response_is_retriable(response):
        status = await resolve_status()
        status_payload = _response_payload(status)
        if _response_succeeded(status) and status_payload.get("status") == "committed":
            return status
    await abort()
    return response


class FileWriteStagingStore:
    """Filesystem-backed, request-independent staging for chunked file writes."""

    def __init__(self, workspace: Path) -> None:
        self.root = workspace / ".skilldev_file_writes"
        self.root.mkdir(parents=True, exist_ok=True)
        self._cleanup_expired()

    @staticmethod
    def _validate_write_id(value: Any) -> str:
        write_id = str(value or "").strip()
        if not _WRITE_ID_RE.fullmatch(write_id):
            raise FileWriteChunkError("invalid write_id")
        return write_id

    def _write_dir(self, write_id: Any) -> Path:
        return self.root / self._validate_write_id(write_id)

    def _cleanup_expired(self) -> None:
        cutoff = time.time() - FILE_WRITE_STAGING_TTL_SECONDS
        for entry in self.root.iterdir():
            try:
                if entry.is_dir() and entry.stat().st_mtime < cutoff:
                    shutil.rmtree(entry)
            except FileNotFoundError:
                continue

    @staticmethod
    def _metadata(params: dict[str, Any]) -> dict[str, Any]:
        try:
            total = int(params.get("total"))
            size_bytes = int(params.get("size_bytes"))
        except (TypeError, ValueError) as exc:
            raise FileWriteChunkError("total and size_bytes must be integers") from exc
        digest = str(params.get("sha256") or "").lower()
        path = str(params.get("path") or "")
        if total <= 0 or total > 1 + FILE_WRITE_MAX_STAGING_BYTES // FILE_WRITE_CHUNK_RAW_BYTES:
            raise FileWriteChunkError("invalid chunk total")
        if size_bytes < 0 or size_bytes > FILE_WRITE_MAX_STAGING_BYTES:
            raise FileWriteChunkError("invalid staged content size")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise FileWriteChunkError("invalid sha256")
        if not path:
            raise FileWriteChunkError("path is required")
        return {"path": path, "total": total, "size_bytes": size_bytes, "sha256": digest}

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise FileWriteChunkError("invalid file write staging metadata") from exc
        if not isinstance(data, dict):
            raise FileWriteChunkError("invalid file write staging metadata")
        return data

    @staticmethod
    def _write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
        tmp = path.with_suffix(f".tmp-{time.time_ns()}")
        try:
            tmp.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
            tmp.replace(path)
        finally:
            tmp.unlink(missing_ok=True)

    def _load_or_create_manifest(self, write_dir: Path, metadata: dict[str, Any]) -> dict[str, Any]:
        manifest_path = write_dir / "manifest.json"
        if manifest_path.exists():
            manifest = self._read_json(manifest_path)
            for key, value in metadata.items():
                if manifest.get(key) != value:
                    raise FileWriteChunkError(f"inconsistent file write {key}")
            return manifest
        write_dir.mkdir(parents=True, exist_ok=True)
        manifest = {**metadata, "status": "pending", "created_at": time.time()}
        self._write_json_atomic(manifest_path, manifest)
        return manifest

    def accept_chunk(self, params: dict[str, Any]) -> dict[str, Any]:
        write_id = self._validate_write_id(params.get("write_id"))
        metadata = self._metadata(params)
        write_dir = self._write_dir(write_id)
        manifest = self._load_or_create_manifest(write_dir, metadata)
        if manifest.get("status") == "committed":
            return dict(manifest.get("result") or {})
        if params.get("encoding") != FILE_WRITE_CHUNK_ENCODING:
            raise FileWriteChunkError("unsupported file write chunk encoding")
        try:
            index = int(params.get("index"))
        except (TypeError, ValueError) as exc:
            raise FileWriteChunkError("chunk index must be an integer") from exc
        if index < 0 or index >= metadata["total"]:
            raise FileWriteChunkError("chunk index out of range")
        data = params.get("data")
        if not isinstance(data, str):
            raise FileWriteChunkError("chunk data must be a base64 string")
        try:
            raw = base64.b64decode(data.encode("ascii"), validate=True)
        except (UnicodeEncodeError, binascii.Error) as exc:
            raise FileWriteChunkError("invalid file write chunk base64") from exc
        if len(raw) > FILE_WRITE_CHUNK_RAW_BYTES:
            raise FileWriteChunkError("file write chunk is too large")
        part_path = write_dir / f"{index:06d}.part"
        if part_path.exists():
            if part_path.read_bytes() != raw:
                raise FileWriteChunkError("duplicate chunk index has different data")
        else:
            part_path.write_bytes(raw)
        manifest_path = write_dir / "manifest.json"
        manifest["updated_at"] = time.time()
        self._write_json_atomic(manifest_path, manifest)
        return {"ok": True, "write_id": write_id, "index": index, "accepted": True}

    def assemble(self, params: dict[str, Any]) -> tuple[bytes | None, dict[str, Any] | None]:
        write_id = self._validate_write_id(params.get("write_id"))
        metadata = self._metadata(params)
        write_dir = self._write_dir(write_id)
        manifest_path = write_dir / "manifest.json"
        if not manifest_path.exists():
            raise FileWriteChunkError("file write staging data not found")
        manifest = self._load_or_create_manifest(write_dir, metadata)
        if manifest.get("status") == "committed":
            return None, dict(manifest.get("result") or {})
        missing = [index for index in range(metadata["total"]) if not (write_dir / f"{index:06d}.part").exists()]
        if missing:
            raise FileWriteChunkError(f"missing file write chunks: {missing}")
        raw = b"".join((write_dir / f"{index:06d}.part").read_bytes() for index in range(metadata["total"]))
        if len(raw) != metadata["size_bytes"]:
            raise FileWriteChunkError("file write content size mismatch")
        if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise FileWriteChunkError("file write content sha256 mismatch")
        return raw, None

    def mark_committed(self, params: dict[str, Any], result: dict[str, Any]) -> dict[str, Any]:
        write_id = self._validate_write_id(params.get("write_id"))
        write_dir = self._write_dir(write_id)
        manifest_path = write_dir / "manifest.json"
        manifest = self._read_json(manifest_path)
        manifest.update({"status": "committed", "result": result, "updated_at": time.time()})
        self._write_json_atomic(manifest_path, manifest)
        return result

    def status(self, params: dict[str, Any]) -> dict[str, Any]:
        write_dir = self._write_dir(params.get("write_id"))
        manifest_path = write_dir / "manifest.json"
        if not manifest_path.exists():
            return {"ok": True, "status": "unknown"}
        manifest = self._read_json(manifest_path)
        if manifest.get("status") == "committed":
            return {**dict(manifest.get("result") or {}), "status": "committed"}
        return {"ok": True, "status": "pending"}

    def abort(self, params: dict[str, Any]) -> dict[str, Any]:
        write_dir = self._write_dir(params.get("write_id"))
        manifest_path = write_dir / "manifest.json"
        if manifest_path.exists():
            manifest = self._read_json(manifest_path)
            if manifest.get("status") == "committed":
                return {**dict(manifest.get("result") or {}), "status": "committed"}
        shutil.rmtree(write_dir, ignore_errors=True)
        return {"ok": True, "status": "aborted"}
