"""Content-addressed audit payloads with explicit, bounded retrieval."""
from __future__ import annotations

import hashlib
import itertools
import json
import os
import re
import uuid
from pathlib import Path
from typing import Any, Iterable

from jiuwenswarm.common.utils import get_agent_sessions_dir
from jiuwenswarm.server.runtime.session import lifecycle as lc

INLINE_BYTES = 64 * 1024
PREVIEW_CHARS = 2048
MAX_CHUNK_BYTES = 64 * 1024
DISPLAY_METADATA_BYTES = 1024 * 1024
ARTIFACT_CURSOR_PREFIX = "history-artifact:v1:"
_ARTIFACT_CURSOR = re.compile(r"history-artifact:v1:([0-9a-f]{64}):(0|[1-9][0-9]{0,17})")
_DISPLAY_FIELDS = ("status", "pending", "skill_tree", "beam_search", "planned_graph", "graph_status", "graph_build")
_DIGEST = re.compile(r"[0-9a-f]{64}")


class ArtifactCopyError(ValueError):
    """A fork cannot retain a complete payload from its source session."""


def json_chunks(value: Any, depth: int = 0) -> Iterable[bytes]:
    """Encode large strings incrementally instead of copying an escaped value."""
    if depth > 64:
        raise ValueError("Artifact JSON nesting exceeds the budget")
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        value = dump(mode="json")
    if isinstance(value, str):
        yield b'"'
        for index in range(0, len(value), 16384):
            yield json.dumps(value[index:index + 16384], ensure_ascii=False)[1:-1].encode("utf-8")
        yield b'"'
    elif isinstance(value, dict):
        yield b"{"
        for index, (key, item) in enumerate(value.items()):
            if index:
                yield b","
            if not isinstance(key, str):
                raise TypeError("Artifact JSON object keys must be strings")
            yield from json_chunks(key, depth + 1)
            yield b":"
            yield from json_chunks(item, depth + 1)
        yield b"}"
    elif isinstance(value, (list, tuple)):
        yield b"["
        for index, item in enumerate(value):
            if index:
                yield b","
            yield from json_chunks(item, depth + 1)
        yield b"]"
    else:
        yield json.dumps(value, ensure_ascii=False, allow_nan=False).encode("utf-8")


def artifact_directory(session_id: str) -> Path:
    return get_agent_sessions_dir() / lc.validate_id(session_id) / "history_payloads"


def put_chunks(session_id: str, chunks: Iterable[bytes], *, media_type: str) -> dict[str, Any]:
    with lc.resource_lock("session", session_id):
        lc.write_guard(session_id)
        return _put_chunks_unfenced(session_id, chunks, media_type=media_type)


def _put_chunks_unfenced(session_id: str, chunks: Iterable[bytes], *, media_type: str) -> dict[str, Any]:
    directory = artifact_directory(session_id)
    directory.mkdir(parents=True, exist_ok=True)
    temporary = directory / f".{uuid.uuid4().hex}.tmp"
    digest = hashlib.sha256()
    length = 0
    try:
        with temporary.open("xb") as stream:
            for chunk in chunks:
                stream.write(chunk)
                digest.update(chunk)
                length += len(chunk)
            stream.flush()
            os.fsync(stream.fileno())
        name = digest.hexdigest()
        path = directory / name
        os.replace(temporary, path)
        return {"sha256": name, "bytes": length, "media_type": media_type,
                "path": str(path)}
    finally:
        temporary.unlink(missing_ok=True)


def put_text(session_id: str, value: str) -> dict[str, Any]:
    return put_chunks(session_id, (value[i:i + 16384].encode("utf-8")
                                  for i in range(0, len(value), 16384)),
                      media_type="text/plain; charset=utf-8")


def put_prompt(session_id: str, messages: Iterable[Any], tools: Iterable[Any]) -> dict[str, Any]:
    """Stream the full messages and tool schemas without joining a prompt copy."""
    return put_chunks(session_id, json_chunks({"messages": list(messages), "tools": list(tools)}),
                      media_type="application/json")


def externalize_payload(session_id: str, payload: dict[str, Any], *, fields: Iterable[str]) -> dict[str, Any]:
    """Keep old inline fields readable; references expose the complete values."""
    result = dict(payload)
    refs = dict(result.get("payload_refs") or {})
    for key in fields:
        if key in refs:
            continue
        value = result.get(key)
        if isinstance(value, str):
            if len(value) <= INLINE_BYTES and len(value.encode("utf-8")) <= INLINE_BYTES:
                continue
            ref = put_text(session_id, value)
            preview = value[:PREVIEW_CHARS]
        elif isinstance(value, (dict, list)) or callable(getattr(value, "model_dump", None)):
            iterator = iter(json_chunks(value))
            prefix = []
            size = 0
            for part in iterator:
                prefix.append(part)
                size += len(part)
                if size > INLINE_BYTES:
                    break
            if size <= INLINE_BYTES:
                continue
            ref = put_chunks(session_id, itertools.chain(prefix, iterator), media_type="application/json")
            preview = "[Structured payload]"
        else:
            continue
        refs[key] = ref
        if key == "raw_output" and isinstance(value, dict):
            result[key] = _display_metadata(value)
        else:
            result[key] = f"{preview}\n[Full {key}: {ref['bytes']} bytes; artifact {ref['sha256']}; {ref['path']}]"
    if refs:
        result["payload_refs"] = refs
    return result


def _display_metadata(value: dict[str, Any]) -> dict[str, Any]:
    """Keep rich-tool projections within the existing raw-output wire budget."""
    result = {"mcp_app": value["mcp_app"]} if "mcp_app" in value else {}
    used = 2
    for chunk in json_chunks(result):
        used += len(chunk)
        if used > DISPLAY_METADATA_BYTES:
            return result
    for key, item in _display_items(value):
        size = len(key) + 4
        for chunk in json_chunks(item):
            size += len(chunk)
            if used + size > DISPLAY_METADATA_BYTES:
                break
        if used + size <= DISPLAY_METADATA_BYTES:
            result[key] = item
            used += size
    return result


def _display_items(value: dict[str, Any]) -> Iterable[tuple[str, Any]]:
    data = value.get("data")
    if isinstance(data, dict):
        states = {key: data[key] for key in ("status", "pending") if key in data}
        if states:
            yield "data", states
    for key in _DISPLAY_FIELDS:
        if key in value:
            yield key, value[key]


def externalize_history_record(session_id: str, record: dict[str, Any]) -> dict[str, Any]:
    event = str(record.get("event_type") or "")
    fields = ["prompt"]
    if event == "chat.tool_result":
        fields.extend(("result", "rendered_result", "raw_output", "content"))
    result = externalize_payload(session_id, record, fields=fields)
    if isinstance(result.get("event_payload"), dict):
        nested = externalize_payload(session_id, result["event_payload"], fields=fields)
        if nested != result["event_payload"]:
            result["event_payload"] = nested
    return result


def read_artifact_chunk(session_id: str, digest: str, *, offset: int = 0,
                        max_bytes: int = MAX_CHUNK_BYTES) -> dict[str, Any]:
    """Read UTF-8 chunks by byte offset, never accepting an arbitrary path."""
    if not isinstance(digest, str) or _DIGEST.fullmatch(digest) is None:
        raise ValueError("Invalid artifact digest")
    if type(offset) is not int or offset < 0:
        raise ValueError("Invalid artifact offset")
    if type(max_bytes) is not int or not 4 <= max_bytes <= MAX_CHUNK_BYTES:
        raise ValueError("Invalid artifact chunk budget")
    with lc.resource_lock("session", session_id):
        lc.guard(session_id)
        path = artifact_directory(session_id) / digest
        with path.open("rb") as stream:
            size = os.fstat(stream.fileno()).st_size
            if offset > size:
                raise ValueError("Artifact offset is past EOF")
            stream.seek(offset)
            raw = stream.read(max_bytes)
        used = len(raw)
        try:
            content = raw.decode("utf-8")
        except UnicodeDecodeError as exc:
            if exc.reason != "unexpected end of data" or offset + len(raw) == size:
                raise ValueError("Artifact is not UTF-8 or offset is not aligned") from exc
            used = exc.start
            content = raw[:used].decode("utf-8")
        return {"sha256": digest, "bytes": size, "content": content,
                "offset": offset, "next_offset": offset + used, "eof": offset + used == size}


def _file_chunks(path: Path) -> Iterable[bytes]:
    with path.open("rb") as stream:
        while True:
            chunk = stream.read(MAX_CHUNK_BYTES)
            if not chunk:
                return
            yield chunk


def read_artifact_cursor(session_id: str, cursor: str) -> dict[str, Any]:
    """Read a chunk using the cursor field forwarded by existing Gateway routes."""
    from jiuwenswarm.server.runtime.session.history_replay import HistoryReplayError

    match = _ARTIFACT_CURSOR.fullmatch(cursor)
    if match is None:
        raise HistoryReplayError("Invalid history artifact cursor", code="HISTORY_ARTIFACT_FAILED")
    try:
        chunk = read_artifact_chunk(session_id, match[1], offset=int(match[2]), max_bytes=8192)
    except (OSError, ValueError, TypeError) as exc:
        raise HistoryReplayError(str(exc), code="HISTORY_ARTIFACT_FAILED") from exc
    return {"messages": [{"event_type": "history.artifact", **chunk}], "next_cursor": None,
            "has_more": False, "snapshot_id": chunk["sha256"], "snapshot_end": chunk["bytes"],
            "total_pages": 1, "page_idx": 1}


def copy_history_artifacts(value: dict[str, Any], source_session_id: str, target_session_id: str) -> dict[str, Any]:
    """Make a fork independent of its parent's lifetime, without loading blobs."""
    replacements = {}

    def copy_refs(item, depth=0):
        if depth > 64:
            raise ArtifactCopyError("History artifact nesting exceeds the copy budget")
        if isinstance(item, dict):
            refs = item.get("payload_refs")
            if isinstance(refs, dict):
                for ref in refs.values():
                    if not isinstance(ref, dict) or not isinstance(ref.get("sha256"), str):
                        raise ArtifactCopyError("Invalid history artifact reference")
                    digest = ref["sha256"]
                    if _DIGEST.fullmatch(digest) is None:
                        raise ArtifactCopyError("Invalid history artifact digest")
                    source = artifact_directory(source_session_id) / digest
                    target = artifact_directory(target_session_id) / digest
                    if not target.exists():
                        copied = put_chunks(
                            target_session_id, _file_chunks(source),
                            media_type=ref.get("media_type", "application/json"),
                        )
                        if copied["sha256"] != digest or copied["bytes"] != ref.get("bytes"):
                            raise ArtifactCopyError("Source history artifact failed its checksum")
                    if isinstance(ref.get("path"), str):
                        replacements[ref["path"]] = str(target)
                    ref["path"] = str(target)
            for key, child in item.items():
                if key != "payload_refs":
                    copy_refs(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                copy_refs(child, depth + 1)

    def rewrite(item):
        if isinstance(item, str):
            for previous, current in replacements.items():
                item = item.replace(previous, current)
            return item
        if isinstance(item, dict):
            return {key: rewrite(child) for key, child in item.items()}
        if isinstance(item, list):
            return [rewrite(child) for child in item]
        return item

    try:
        copy_refs(value)
        return rewrite(value)
    except ArtifactCopyError:
        raise
    except Exception as exc:
        raise ArtifactCopyError("A source history artifact could not be copied") from exc
