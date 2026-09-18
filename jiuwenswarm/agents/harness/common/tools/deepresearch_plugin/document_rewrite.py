# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Deterministic provenance checks and immutable DeepResearch revisions."""
from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import stat
import tempfile
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator
from urllib.parse import quote

from markdown_it import MarkdownIt

from jiuwenswarm.agents.harness.common.tools.deepresearch.path_safety import (
    is_direct_directory,
)
from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.artifact_naming import (
    ArtifactNamingError,
    ArtifactPaths,
    allocate_next_paths,
)

from jiuwenswarm.agents.harness.common.tools.deepresearch_plugin.markdown_rewrite_map import (
    MarkdownRewriteMap,
    ProtectedAnchor,
    RewriteMapError,
    RewriteSlot,
    RewriteUnit,
    Utf8BoundaryTable,
    build_document_anchor_index,
    build_rewrite_map,
    reconstruct_markdown,
    structure_signature,
    visible_slot_byte_ranges,
)

logger = logging.getLogger(__name__)

SAFE_ID_RE = re.compile(r"^(?:doc|rev)_[A-Za-z0-9_-]{1,128}$")
CITATION_RE = re.compile(r"\[\[(?P<index>\d+)\]\]\((?P<url>https?://[^\s)]+)\)")
FORBIDDEN_OUTPUT_RE = re.compile(
    r"(?<![A-Za-z0-9+.-])[A-Za-z][A-Za-z0-9+.-]*://|"
    r"(?<![A-Za-z0-9+.-])(?:javascript|vbscript|data|mailto|tel|sms|urn):[ \t\r\n]*|"
    r"!\[|\]\s*(?:\(|\[)|"
    r"^\s*\[[^\]\r\n]+\]\s*:\s*\S+|"
    r"<[A-Za-z!/][^>]*>|#inference:",
    re.IGNORECASE | re.MULTILINE,
)
LIST_RE = re.compile(r"^\s*(?:[-+*]|\d+[.)])\s+")
TOKEN_TTL_SECONDS = 10 * 60
CONTEXT_CACHE_MAX = 1024
PROVENANCE_MAX_BYTES = 8 * 1024 * 1024
FINAL_RESULT_MAX_BYTES = 64 * 1024 * 1024
MARKDOWN_MAX_BYTES = 64 * 1024 * 1024
CITATION_COUNT_MAX = 10_000
CITATION_FIELD_MAX_BYTES = 1024 * 1024
MAX_HIGHLIGHT_RANGES = 4096
MAX_PUBLICATION_ATTEMPTS = 16
_BOUNDARY_PUNCTUATION = "，。！？；：,.!?;:"
_INLINE_PARSER = MarkdownIt("commonmark")
_VERSIONING_ERROR_MESSAGE = "report artifact versioning failed"


class RewriteError(ValueError):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True, slots=True)
class RewriteBlock:
    block_id: str
    start: int
    end: int
    text: str


@dataclass(slots=True)
class _RewriteContext:
    expires_at: float
    session_id: str
    workspace_root: Path
    report_path: Path
    provenance_sha256: str
    document_id: str
    parent_revision_id: str
    final_result_path: str
    final_result_sha256: str
    parent_hash: str
    selection_start_byte: int
    selection_end_byte: int
    selected_units: tuple["_SelectedUnitRange", ...]
    structure_signature: tuple[object, ...]
    action: str


@dataclass(frozen=True, slots=True)
class _SelectedSlotRange:
    slot_id: str
    start_byte: int
    end_byte: int


@dataclass(frozen=True, slots=True)
class _SelectedUnitRange:
    unit_id: str
    start_byte: int
    end_byte: int
    slots: tuple[_SelectedSlotRange, ...]


@dataclass(frozen=True, slots=True)
class _CoveredSelection:
    units: tuple[RewriteUnit, ...]
    first_index: int
    last_index: int


@dataclass(slots=True)
class _DocumentLockEntry:
    lock: threading.Lock
    references: int = 0


_CONTEXTS: dict[str, _RewriteContext] = {}
_CONTEXT_LOCK = threading.Lock()
_DOCUMENT_LOCKS: dict[str, _DocumentLockEntry] = {}


@contextmanager
def _document_lock(document_id: str) -> Iterator[None]:
    with _CONTEXT_LOCK:
        entry = _DOCUMENT_LOCKS.get(document_id)
        if entry is None:
            entry = _DocumentLockEntry(threading.Lock())
            _DOCUMENT_LOCKS[document_id] = entry
        entry.references += 1
    acquired = False
    try:
        entry.lock.acquire()
        acquired = True
        yield
    finally:
        if acquired:
            entry.lock.release()
        with _CONTEXT_LOCK:
            entry.references -= 1
            if (
                entry.references == 0
                and _DOCUMENT_LOCKS.get(document_id) is entry
            ):
                del _DOCUMENT_LOCKS[document_id]


def _sha256(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _block_id(start: int, text: str) -> str:
    digest = 0x811C9DC5
    for byte in text.encode("utf-8"):
        digest ^= byte
        digest = (digest * 0x01000193) & 0xFFFFFFFF
    return f"block_{start}_{digest:08x}"


def _is_unsupported_line(line: str) -> bool:
    stripped = line.lstrip()
    return (
        stripped.startswith("#")
        or stripped.startswith(">")
        or stripped.startswith("<")
        or stripped.startswith("```")
        or stripped.startswith("~~~")
        or "|" in stripped
        or stripped.startswith("![")
    )


def iter_rewrite_blocks(markdown: str) -> Iterator[RewriteBlock]:
    """Yield source-addressable plain paragraphs and single list items."""
    lines = markdown.splitlines(keepends=True)
    offset = 0
    index = 0
    in_fence = False
    while index < len(lines):
        line = lines[index]
        stripped = line.strip()
        if line.lstrip().startswith(("```", "~~~")):
            in_fence = not in_fence
            offset += len(line)
            index += 1
            continue
        if in_fence or not stripped or _is_unsupported_line(line):
            offset += len(line)
            index += 1
            continue

        start = offset
        if LIST_RE.match(line):
            text = line.rstrip("\r\n")
            offset += len(line)
            index += 1
        else:
            parts: list[str] = []
            while index < len(lines):
                candidate = lines[index]
                if not candidate.strip() or _is_unsupported_line(candidate) or LIST_RE.match(candidate):
                    break
                parts.append(candidate)
                offset += len(candidate)
                index += 1
            text = "".join(parts).rstrip("\r\n")
        if text:
            yield RewriteBlock(_block_id(start, text), start, start + len(text), text)


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _lexical_absolute(path: str | Path, *, base: Path | None = None) -> Path:
    raw = os.path.expanduser(os.fspath(path))
    if not os.path.isabs(raw):
        raw = os.path.join(os.fspath(base) if base is not None else os.getcwd(), raw)
    return Path(os.path.abspath(raw))


def _lexical_workspace_root(path: str | Path) -> Path:
    """Normalize a trusted root without following its final path component."""
    return _lexical_absolute(path)


def _same_file_identity(left: os.stat_result, right: os.stat_result) -> bool:
    return (left.st_dev, left.st_ino) == (right.st_dev, right.st_ino)


def _validate_regular_read_target(
    named: os.stat_result,
    opened: os.stat_result,
    limit: int,
    label: str,
) -> None:
    valid = (
        _same_file_identity(named, opened)
        and stat.S_ISREG(named.st_mode)
        and stat.S_ISREG(opened.st_mode)
        and named.st_nlink == 1
        and opened.st_nlink == 1
        and named.st_size <= limit
        and opened.st_size <= limit
    )
    if not valid:
        raise OSError(f"{label} is not a safe bounded regular file")


def _read_descriptor_bounded(descriptor: int, limit: int, label: str) -> bytes:
    chunks: list[bytes] = []
    remaining = limit + 1
    while remaining:
        chunk = os.read(descriptor, min(remaining, 1024 * 1024))
        if not chunk:
            break
        chunks.append(chunk)
        remaining -= len(chunk)
    payload = b"".join(chunks)
    if len(payload) > limit:
        raise OSError(f"{label} exceeds the read limit")
    return payload


def _read_regular_bounded_posix(
    path: Path, root: Path, relative: Path, limit: int, label: str
) -> bytes:
    directory_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        directory_flags |= nofollow
    named_root = os.lstat(root)
    if not is_direct_directory(named_root):
        raise OSError(f"{label} root must be a direct directory")
    root_descriptor = os.open(root, directory_flags, mode=0o700)
    opened_root = os.fstat(root_descriptor)
    if not _same_file_identity(named_root, opened_root) or not is_direct_directory(
        opened_root
    ):
        os.close(root_descriptor)
        raise OSError(f"{label} root changed during open")
    parent_descriptor = root_descriptor
    descriptor: int | None = None
    try:
        components = relative.parts
        if not components:
            raise OSError(f"{label} path has no leaf")
        for component in components[:-1]:
            named = os.stat(
                component,
                dir_fd=parent_descriptor,
                follow_symlinks=False,
            )
            if not is_direct_directory(named):
                raise OSError(f"{label} parent must be a direct directory")
            opened_descriptor = os.open(
                component,
                directory_flags,
                dir_fd=parent_descriptor,
            )
            opened = os.fstat(opened_descriptor)
            if not _same_file_identity(named, opened) or not is_direct_directory(
                opened
            ):
                os.close(opened_descriptor)
                raise OSError(f"{label} parent changed during open")
            if parent_descriptor != root_descriptor:
                os.close(parent_descriptor)
            parent_descriptor = opened_descriptor

        leaf = components[-1]
        named_before = os.stat(
            leaf,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        invalid_named_file = (
            stat.S_ISLNK(named_before.st_mode)
            or not stat.S_ISREG(named_before.st_mode)
            or named_before.st_nlink != 1
            or named_before.st_size > limit
        )
        if invalid_named_file:
            raise OSError(f"{label} is not a safe bounded regular file")
        flags = os.O_RDONLY
        if hasattr(os, "O_BINARY"):
            flags |= os.O_BINARY
        if nofollow is not None:
            flags |= nofollow
        descriptor = os.open(leaf, flags, dir_fd=parent_descriptor)
        opened_before = os.fstat(descriptor)
        _validate_regular_read_target(
            named_before, opened_before, limit, label
        )
        payload = _read_descriptor_bounded(descriptor, limit, label)
        opened_after = os.fstat(descriptor)
        named_after = os.stat(
            leaf,
            dir_fd=parent_descriptor,
            follow_symlinks=False,
        )
        _validate_regular_read_target(named_after, opened_after, limit, label)
        if (
            not _same_file_identity(opened_before, opened_after)
            or opened_before.st_size != opened_after.st_size
            or len(payload) != opened_after.st_size
        ):
            raise OSError(f"{label} changed during read")
        return payload
    finally:
        if descriptor is not None:
            os.close(descriptor)
        if parent_descriptor != root_descriptor:
            os.close(parent_descriptor)
        os.close(root_descriptor)


def _read_regular_bounded_fallback(
    path: Path, root: Path, relative: Path, limit: int, label: str
) -> bytes:
    root_metadata = os.lstat(root)
    if not is_direct_directory(root_metadata):
        raise OSError(f"{label} root must be a direct directory")
    directory_chain = [(root, (root_metadata.st_dev, root_metadata.st_ino))]
    current = root
    for component in relative.parts[:-1]:
        current = current / component
        metadata = os.lstat(current)
        if not is_direct_directory(metadata):
            raise OSError(f"{label} parent must be a direct directory")
        directory_chain.append((current, (metadata.st_dev, metadata.st_ino)))
    named_before = os.lstat(path)
    invalid_named_file = (
        stat.S_ISLNK(named_before.st_mode)
        or not stat.S_ISREG(named_before.st_mode)
        or named_before.st_nlink != 1
        or named_before.st_size > limit
    )
    if invalid_named_file:
        raise OSError(f"{label} is not a safe bounded regular file")
    flags = os.O_RDONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags)
    try:
        opened_before = os.fstat(descriptor)
        _validate_regular_read_target(named_before, opened_before, limit, label)
        payload = _read_descriptor_bounded(descriptor, limit, label)
        opened_after = os.fstat(descriptor)
        named_after = os.lstat(path)
        _validate_regular_read_target(named_after, opened_after, limit, label)
        if (
            not _same_file_identity(opened_before, opened_after)
            or opened_before.st_size != opened_after.st_size
            or len(payload) != opened_after.st_size
        ):
            raise OSError(f"{label} changed during read")
        for directory, expected_identity in directory_chain:
            metadata = os.lstat(directory)
            if (
                not is_direct_directory(metadata)
                or (metadata.st_dev, metadata.st_ino) != expected_identity
            ):
                raise OSError(f"{label} parent changed during read")
        return payload
    finally:
        os.close(descriptor)


def _read_regular_bounded_beneath(
    path: str | Path,
    root: Path,
    limit: int,
    label: str,
) -> bytes:
    if type(limit) is not int or limit < 0:
        raise OSError(f"{label} has an invalid read limit")
    trusted_root = _lexical_workspace_root(root)
    lexical_path = _lexical_absolute(path)
    if not _inside(lexical_path, trusted_root):
        raise OSError(f"{label} is outside the trusted workspace")
    relative = lexical_path.relative_to(trusted_root)
    supports_secure_dirfd = (
        os.open in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_follow_symlinks", set())
    )
    if supports_secure_dirfd:
        return _read_regular_bounded_posix(
            lexical_path, trusted_root, relative, limit, label
        )
    return _read_regular_bounded_fallback(
        lexical_path, trusted_root, relative, limit, label
    )


def _load_provenance(
    report_path: Path, root: Path | None = None
) -> tuple[dict, str]:
    sidecar = report_path.with_suffix(".provenance.json")
    trusted_root = root if root is not None else report_path.parent
    try:
        payload = _read_regular_bounded_beneath(
            sidecar, trusted_root, PROVENANCE_MAX_BYTES, "report provenance"
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report provenance is unavailable") from exc
    try:
        value = json.loads(payload)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report provenance is unavailable") from exc
    if not isinstance(value, dict):
        raise RewriteError("DOCUMENT_NOT_FOUND", "report provenance is invalid")
    return value, _sha256(payload)


def _load_final_result_snapshot(
    report_path: Path, provenance: dict, root: Path
) -> object:
    raw_path = provenance.get("final_result_path")
    expected_hash = provenance.get("final_result_sha256")
    if not isinstance(raw_path, str) or not raw_path or not isinstance(expected_hash, str):
        raise RewriteError("DOCUMENT_NOT_FOUND", "report final result is unavailable")
    try:
        snapshot_path = _lexical_absolute(raw_path, base=report_path.parent)
    except (TypeError, OSError, ValueError) as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report final result is unavailable") from exc
    if not _inside(snapshot_path, root):
        raise RewriteError("BAD_REQUEST", "report final result is outside the current workspace")
    try:
        snapshot_bytes = _read_regular_bounded_beneath(
            snapshot_path,
            root,
            FINAL_RESULT_MAX_BYTES,
            "report final result",
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report final result is unavailable") from exc
    if _sha256(snapshot_bytes) != expected_hash:
        raise RewriteError("REVISION_CONFLICT", "the report final result changed")
    try:
        snapshot = json.loads(snapshot_bytes)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report final result is invalid") from exc
    return snapshot


def _load_final_result_citations(report_path: Path, provenance: dict, root: Path) -> list[dict]:
    snapshot = _load_final_result_snapshot(report_path, provenance, root)
    citation_messages = snapshot.get("citation_messages") if isinstance(snapshot, dict) else None
    citations = citation_messages.get("data") if isinstance(citation_messages, dict) else None
    if (
        not isinstance(citations, list)
        or len(citations) > CITATION_COUNT_MAX
        or any(not isinstance(item, dict) for item in citations)
    ):
        raise RewriteError("DOCUMENT_NOT_FOUND", "report citation data is invalid")
    _citation_index(citations)
    return citations


def prepare_html_export(
    *,
    workspace_root: str | Path,
    report_path: str | Path,
    revision_id: str,
) -> dict:
    try:
        root = _lexical_workspace_root(workspace_root)
        report = _lexical_absolute(report_path)
    except (TypeError, OSError, RuntimeError, ValueError) as exc:
        raise RewriteError("BAD_REQUEST", "invalid HTML export request") from exc
    valid_report_path = _inside(report, root) and report.suffix.lower() == ".md"
    valid_revision_id = (
        isinstance(revision_id, str)
        and re.fullmatch(r"rev_[A-Za-z0-9_-]{1,128}", revision_id) is not None
    )
    if not valid_report_path or not valid_revision_id:
        raise RewriteError("BAD_REQUEST", "invalid HTML export request")

    try:
        markdown_bytes = _read_regular_bounded_beneath(
            report, root, MARKDOWN_MAX_BYTES, "report markdown"
        )
        markdown = markdown_bytes.decode("utf-8")
        provenance, _ = _load_provenance(report, root)
        raw_markdown_path = provenance.get("markdown_path")
        if not isinstance(raw_markdown_path, str) or not raw_markdown_path:
            raise RewriteError("REVISION_CONFLICT", "rewrite export source changed")
        provenance_markdown_path = _lexical_absolute(
            raw_markdown_path, base=report.parent
        )
        parent_revision_id = provenance.get("parent_revision_id")
        valid_parent_revision_id = (
            isinstance(parent_revision_id, str)
            and re.fullmatch(r"rev_[A-Za-z0-9_-]{1,128}", parent_revision_id)
            is not None
        )
        valid_protocol = (
            type(provenance.get("rewrite_protocol_version")) is int
            and provenance.get("rewrite_protocol_version") == 2
        )
        valid_history = (
            isinstance(provenance.get("rewrite_history"), list)
            and bool(provenance["rewrite_history"])
        )
        provenance_matches_report = (
            provenance_markdown_path == report
            and provenance.get("revision_id") == revision_id
            and provenance.get("content_sha256") == _sha256(markdown_bytes)
        )
        valid_provenance_state = (
            provenance_matches_report
            and valid_parent_revision_id
            and valid_protocol
            and valid_history
        )
        if not valid_provenance_state:
            raise RewriteError("REVISION_CONFLICT", "rewrite export source changed")
        snapshot = _load_final_result_snapshot(report, provenance, root)
        if not isinstance(snapshot, dict):
            raise RewriteError("REVISION_CONFLICT", "rewrite export source changed")
    except (
        OSError,
        TypeError,
        RuntimeError,
        ValueError,
    ) as exc:
        raise RewriteError("REVISION_CONFLICT", "rewrite export source changed") from exc

    style_input = dict(snapshot)
    style_input["response_content"] = markdown
    return {"report_path": str(report), "final_result": style_input}


def _citation_index(
    citations: list[dict],
) -> tuple[dict[str, dict], dict[str, tuple[dict, ...]]]:
    def field_size(value: str) -> int:
        try:
            return len(value.encode("utf-8"))
        except UnicodeEncodeError as exc:
            raise RewriteError(
                "DOCUMENT_NOT_FOUND", "report citation data is invalid"
            ) from exc

    by_id: dict[str, dict] = {}
    by_key_lists: dict[str, list[dict]] = {}
    for item in citations:
        raw_id = item.get("id")
        raw_reference_index = item.get("reference_index")
        url = item.get("url")
        invalid_citation_identity = (
            isinstance(raw_id, bool)
            or not isinstance(raw_id, (str, int))
            or isinstance(raw_reference_index, bool)
            or not isinstance(raw_reference_index, (str, int))
            or not isinstance(url, str)
        )
        if invalid_citation_identity:
            raise RewriteError("DOCUMENT_NOT_FOUND", "report citation data is invalid")
        source_id = str(raw_id).strip()
        reference_index = str(raw_reference_index).strip()
        url = url.strip()
        valid_url = re.fullmatch(r"https?://\S+", url, re.IGNORECASE) is not None
        field_too_large = any(
            field_size(value) > CITATION_FIELD_MAX_BYTES
            for value in (source_id, reference_index, url)
        )
        required_fields_present = bool(source_id and reference_index and url)
        if not required_fields_present or not valid_url or field_too_large:
            raise RewriteError("DOCUMENT_NOT_FOUND", "report citation data is invalid")
        for field in ("title", "content", "chunk", "source"):
            value = item.get(field)
            if value is not None and (
                not isinstance(value, str)
                or field_size(value) > CITATION_FIELD_MAX_BYTES
            ):
                raise RewriteError("DOCUMENT_NOT_FOUND", "report citation data is invalid")
        key = f"{reference_index}\0{url}"
        if source_id in by_id:
            raise RewriteError("DOCUMENT_NOT_FOUND", "report citation data is ambiguous")
        by_id[source_id] = item
        by_key_lists.setdefault(key, []).append(item)
    return by_id, {key: tuple(items) for key, items in by_key_lists.items()}


def _citation_occurrence_index(
    markdown: str, citations: list[dict]
) -> dict[tuple[int, int], dict]:
    _, by_key = _citation_index(citations)
    boundary_table = Utf8BoundaryTable(markdown)
    ranges_by_key: dict[str, list[tuple[int, int]]] = {}
    for match in CITATION_RE.finditer(markdown):
        key = f"{match.group('index')}\0{match.group('url')}"
        ranges_by_key.setdefault(key, []).append(
            (
                boundary_table.codepoint_to_byte[match.start()],
                boundary_table.codepoint_to_byte[match.end()],
            )
        )

    result: dict[tuple[int, int], dict] = {}
    for key, items in by_key.items():
        ranges = ranges_by_key.get(key, [])
        if len(items) == 1:
            for byte_range in ranges:
                result[byte_range] = items[0]
            continue
        if len(ranges) != len(items):
            raise RewriteError("DOCUMENT_NOT_FOUND", "report citation data is ambiguous")
        result.update(zip(ranges, items))
    return result


def _mapping_conflict(message: str) -> None:
    raise RewriteError("SELECTION_MAPPING_CONFLICT", message)


def _intersects(start: int, end: int, other_start: int, other_end: int) -> bool:
    return start < other_end and other_start < end


def _require_selection_protocol(selection: object) -> dict:
    if not isinstance(selection, dict):
        raise RewriteError(
            "SELECTION_PROTOCOL_UNSUPPORTED", "selection protocol version 2 is required"
        )
    version = selection.get("protocol_version")
    if type(version) is not int or version != 2:
        raise RewriteError(
            "SELECTION_PROTOCOL_UNSUPPORTED", "selection protocol version 2 is required"
        )
    return selection


def _validate_selection_request(selection: object) -> tuple[int, int, str, str]:
    selection = _require_selection_protocol(selection)
    start = selection.get("start_byte")
    end = selection.get("end_byte")
    selected_text = selection.get("selected_text")
    source_hash = selection.get("source_sha256")
    valid_byte_range = type(start) is int and type(end) is int and start >= 0 and end > start
    if not valid_byte_range:
        _mapping_conflict("selection byte range is invalid")
    if not isinstance(selected_text, str):
        _mapping_conflict("selected text must be a string")
    if not selected_text or len(selected_text) > 12_000:
        raise RewriteError("BAD_REQUEST", "selection size is invalid")
    if (
        not isinstance(source_hash, str)
        or re.fullmatch(r"[0-9a-f]{64}", source_hash) is None
    ):
        _mapping_conflict("selection source hash is invalid")
    return start, end, selected_text, source_hash


def _validate_selection(selection: object, markdown: str) -> tuple[int, int, str]:
    start, end, selected_text, source_hash = _validate_selection_request(selection)
    boundary_table = Utf8BoundaryTable(markdown)
    try:
        boundary_table.require_byte_boundary(start)
        boundary_table.require_byte_boundary(end)
    except ValueError as exc:
        _mapping_conflict(str(exc))
    source_bytes = markdown.encode("utf-8")
    if end > len(source_bytes) or _sha256(source_bytes[start:end]) != source_hash:
        _mapping_conflict("selection source hash does not match the document")
    return start, end, selected_text


def _slot_slice(slot: RewriteSlot, start: int, end: int) -> tuple[str, int, int] | None:
    boundaries = slot.visible_boundary_to_byte
    selected_start = max(start, slot.start_byte)
    selected_end = min(end, slot.end_byte)
    if selected_start >= selected_end:
        return None
    try:
        visible_start = boundaries.index(selected_start)
        visible_end = boundaries.index(selected_end)
    except ValueError:
        _mapping_conflict("selection endpoint is not a visible text boundary")
    return slot.text[visible_start:visible_end], selected_start, selected_end


def _anchor_visible_text(anchor: ProtectedAnchor) -> str:
    if anchor.kind == "citation":
        match = CITATION_RE.fullmatch(anchor.source)
        return f"[{match.group('index')}]" if match else ""
    if anchor.kind == "hard_break":
        return "\n"
    if anchor.kind == "inline_code":
        parsed = _INLINE_PARSER.parseInline(anchor.source)
        children = parsed[0].children if parsed else None
        if children and len(children) == 1 and children[0].type == "code_inline":
            return children[0].content
    return ""


def _unit_visible_text(unit: RewriteUnit, start: int, end: int) -> str:
    events: list[tuple[int, int, str]] = []
    for slot in unit.slots:
        sliced = _slot_slice(slot, start, end)
        if sliced is not None:
            text, selected_start, selected_end = sliced
            events.append((selected_start, selected_end, text))
    for anchor in unit.protected:
        if _intersects(start, end, anchor.start_byte, anchor.end_byte):
            events.append((anchor.start_byte, anchor.end_byte, _anchor_visible_text(anchor)))
    return "".join(item[2] for item in sorted(events))


def _full_unit_visible_text(unit: RewriteUnit) -> str:
    return _unit_visible_text(unit, unit.start_byte, unit.end_byte)


def _endpoint_is_visible(units: tuple[RewriteUnit, ...], endpoint: int) -> bool:
    return any(
        endpoint in slot.visible_boundary_to_byte
        for unit in units
        for slot in unit.slots
    )


def _selected_unit_payload(unit: RewriteUnit, start: int, end: int) -> dict:
    slots = []
    for slot in unit.slots:
        sliced = _slot_slice(slot, start, end)
        if sliced is None:
            continue
        text, _, _ = sliced
        value = {
            "slot_id": slot.slot_id,
            "text": text,
            "format": list(slot.formats),
        }
        if slot.link_id is not None:
            value["link_id"] = slot.link_id
        slots.append(value)
    return {
        "unit_id": unit.unit_id,
        "type": unit.unit_type,
        "level": unit.level,
        "list_depth": unit.list_depth,
        "list_marker": unit.list_marker,
        "slots": slots,
    }


def _selected_unit_range(
    unit: RewriteUnit, start: int, end: int
) -> _SelectedUnitRange:
    slots = []
    for slot in unit.slots:
        sliced = _slot_slice(slot, start, end)
        if sliced is not None:
            _, selected_start, selected_end = sliced
            slots.append(
                _SelectedSlotRange(slot.slot_id, selected_start, selected_end)
            )
    return _SelectedUnitRange(
        unit.unit_id,
        max(start, unit.start_byte),
        min(end, unit.end_byte),
        tuple(slots),
    )


def _selected_units(
    rewrite_map: MarkdownRewriteMap, start: int, end: int, provenance: dict
) -> _CoveredSelection:
    for region in rewrite_map.unsupported_regions:
        if _intersects(start, end, region.start_byte, region.end_byte):
            raise RewriteError("UNSUPPORTED_SELECTION", "selection crosses unsupported Markdown")
    covered_pairs = tuple(
        (index, unit)
        for index, unit in enumerate(rewrite_map.units)
        if _intersects(start, end, unit.start_byte, unit.end_byte)
    )
    covered = tuple(unit for _, unit in covered_pairs)
    if not covered:
        raise RewriteError("UNSUPPORTED_SELECTION", "selection does not cover editable Markdown")

    protected = tuple(anchor for unit in covered for anchor in unit.protected)
    if any(
        anchor.kind == "inference"
        and _intersects(start, end, anchor.start_byte, anchor.end_byte)
        for anchor in protected
    ):
        raise RewriteError(
            "INFERENCE_REWRITE_UNSUPPORTED", "inference-linked text cannot be rewritten"
        )
    inference_paths = {
        item.get("path")
        for item in provenance.get("inference_manifest") or []
        if isinstance(item, dict) and isinstance(item.get("path"), str) and item.get("path")
    }
    if any(
        anchor.kind == "link_destination"
        and _intersects(start, end, anchor.start_byte, anchor.end_byte)
        and _link_destination_href(anchor) in inference_paths
        for anchor in protected
    ):
        raise RewriteError(
            "INFERENCE_REWRITE_UNSUPPORTED", "inference-linked text cannot be rewritten"
        )
    if any(
        anchor.kind == "image"
        and _intersects(start, end, anchor.start_byte, anchor.end_byte)
        for anchor in protected
    ):
        raise RewriteError("UNSUPPORTED_SELECTION", "images cannot be rewritten")
    if not _endpoint_is_visible(covered, start) or not _endpoint_is_visible(covered, end):
        raise RewriteError(
            "UNSUPPORTED_SELECTION", "selection endpoint is not an editable visible boundary"
        )

    indexes = [index for index, _ in covered_pairs]
    if indexes != list(range(indexes[0], indexes[-1] + 1)):
        raise RewriteError("UNSUPPORTED_SELECTION", "selected units are not continuous")
    for unit in covered[1:-1]:
        if not unit.slots:
            raise RewriteError("UNSUPPORTED_SELECTION", "middle unit is not editable")
        editable_start = min(slot.visible_boundary_to_byte[0] for slot in unit.slots)
        editable_end = max(slot.visible_boundary_to_byte[-1] for slot in unit.slots)
        if start > editable_start or end < editable_end:
            raise RewriteError("UNSUPPORTED_SELECTION", "middle unit is only partially selected")
    list_depths = {unit.list_depth for unit in covered if unit.unit_type == "list_item"}
    if len(list_depths) > 1:
        raise RewriteError("UNSUPPORTED_SELECTION", "selected list items have different depths")
    return _CoveredSelection(covered, indexes[0], indexes[-1])


def _link_destination_href(anchor: ProtectedAnchor) -> str | None:
    if anchor.kind != "link_destination":
        return None
    parsed = _INLINE_PARSER.parseInline(f"[label{anchor.source}")
    children = parsed[0].children if parsed else None
    if not children:
        return None
    link_open = next((token for token in children if token.type == "link_open"), None)
    href = link_open.attrGet("href") if link_open is not None else None
    return href if isinstance(href, str) and href else None


def _has_unsupported_gap(
    rewrite_map: MarkdownRewriteMap, left_byte: int, right_byte: int
) -> bool:
    return any(
        region.start_byte < right_byte and region.end_byte > left_byte
        for region in rewrite_map.unsupported_regions
    )


def prepare_rewrite(
    *,
    workspace_root: str | Path,
    report_path: str | Path,
    action: str,
    selection: object,
    instruction: str = "",
    session_id: str,
) -> dict:
    root = _lexical_workspace_root(workspace_root)
    report = _lexical_absolute(report_path)
    _require_selection_protocol(selection)
    if not _inside(report, root) or report.suffix.lower() != ".md":
        raise RewriteError("BAD_REQUEST", "report path is outside the current workspace")
    if action not in {"shorten", "expand", "polish"}:
        raise RewriteError("BAD_REQUEST", "unsupported rewrite action")
    if not isinstance(instruction, str) or len(instruction) > 2_000:
        raise RewriteError("BAD_REQUEST", "instruction size is invalid")
    _validate_selection_request(selection)

    try:
        report_bytes = _read_regular_bounded_beneath(
            report, root, MARKDOWN_MAX_BYTES, "report markdown"
        )
    except (OSError, TypeError, ValueError) as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report is unavailable") from exc
    try:
        markdown = report_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report is unavailable") from exc
    provenance, provenance_sha256 = _load_provenance(report, root)
    document_id = provenance.get("document_id")
    revision_id = provenance.get("revision_id")
    content_sha256 = provenance.get("content_sha256")
    valid_document_id = isinstance(document_id, str) and SAFE_ID_RE.fullmatch(document_id)
    valid_revision_id = isinstance(revision_id, str) and SAFE_ID_RE.fullmatch(revision_id)
    valid_content_hash = (
        isinstance(content_sha256, str)
        and re.fullmatch(r"[a-fA-F0-9]{64}", content_sha256)
    )
    if not valid_document_id or not valid_revision_id or not valid_content_hash:
        raise RewriteError("DOCUMENT_NOT_FOUND", "report provenance is invalid")
    actual_hash = _sha256(report_bytes)
    if actual_hash != content_sha256:
        raise RewriteError("REVISION_CONFLICT", "the report revision changed")
    final_result_citations = _load_final_result_citations(report, provenance, root)

    start, end, selected_text = _validate_selection(selection, markdown)
    rewrite_map = build_rewrite_map(markdown)
    covered_selection = _selected_units(rewrite_map, start, end, provenance)
    covered = covered_selection.units
    if not any(
        _slot_slice(slot, start, end) is not None
        for unit in covered
        for slot in unit.slots
    ):
        raise RewriteError(
            "UNSUPPORTED_SELECTION", "selection contains no editable text"
        )
    normalized_visible = "\n".join(
        _unit_visible_text(unit, start, end) for unit in covered
    ).replace("\r\n", "\n").replace("\r", "\n")
    if normalized_visible != selected_text.replace("\r\n", "\n").replace("\r", "\n"):
        _mapping_conflict("selected text does not match normalized Markdown visibility")

    citation_occurrences = _citation_occurrence_index(markdown, final_result_citations)
    allowed: dict[str, dict] = {}
    selected_anchors = []
    for unit in covered:
        for anchor in unit.protected:
            if _intersects(start, end, anchor.start_byte, anchor.end_byte):
                selected_anchors.append(anchor)
    for anchor in selected_anchors:
        if anchor.kind != "citation":
            continue
        match = CITATION_RE.fullmatch(anchor.source)
        if match is not None:
            citation = citation_occurrences.get((anchor.start_byte, anchor.end_byte))
            if citation is not None:
                allowed[str(citation.get("id"))] = citation

    first_index = covered_selection.first_index
    last_index = covered_selection.last_index
    previous = rewrite_map.units[first_index - 1] if first_index else None
    next_unit = (
        rewrite_map.units[last_index + 1]
        if last_index + 1 < len(rewrite_map.units)
        else None
    )
    if previous is not None and _has_unsupported_gap(
        rewrite_map, previous.end_byte, covered[0].start_byte
    ):
        previous = None
    if next_unit is not None and _has_unsupported_gap(
        rewrite_map, covered[-1].end_byte, next_unit.start_byte
    ):
        next_unit = None
    unit_payloads = tuple(_selected_unit_payload(unit, start, end) for unit in covered)
    selected_unit_ranges = tuple(
        _selected_unit_range(unit, start, end) for unit in covered
    )

    token = uuid.uuid4().hex
    context = _RewriteContext(
        expires_at=time.monotonic() + TOKEN_TTL_SECONDS,
        session_id=session_id,
        workspace_root=root,
        report_path=report,
        provenance_sha256=provenance_sha256,
        document_id=document_id,
        parent_revision_id=revision_id,
        final_result_path=str(provenance["final_result_path"]),
        final_result_sha256=str(provenance["final_result_sha256"]),
        parent_hash=actual_hash,
        selection_start_byte=start,
        selection_end_byte=end,
        selected_units=selected_unit_ranges,
        structure_signature=structure_signature(rewrite_map),
        action=action,
    )
    _store_context(token, context)
    return {
        "context_token": token,
        "action_category": "synonym_rewrite",
        "action": action,
        "units": list(unit_payloads),
        "readonly_context": {
            "previous_unit": _full_unit_visible_text(previous) if previous else None,
            "next_unit": _full_unit_visible_text(next_unit) if next_unit else None,
        },
        "instruction": instruction,
        "allowed_source_ids": sorted(allowed),
        "citation_evidence": [
            {name: allowed[key].get(name) for name in ("id", "title", "content", "chunk", "source")}
            for key in sorted(allowed)
        ],
    }


def _sweep_contexts_locked(now: float) -> None:
    for token in [
        token for token, context in _CONTEXTS.items() if context.expires_at <= now
    ]:
        _CONTEXTS.pop(token, None)


def _store_context(token: str, context: _RewriteContext) -> None:
    with _CONTEXT_LOCK:
        _sweep_contexts_locked(time.monotonic())
        if CONTEXT_CACHE_MAX < 1:
            raise RewriteError("BAD_REQUEST", "rewrite context cache is unavailable")
        if len(_CONTEXTS) >= CONTEXT_CACHE_MAX:
            evicted = min(
                _CONTEXTS,
                key=lambda item: (_CONTEXTS[item].expires_at, item),
            )
            _CONTEXTS.pop(evicted, None)
        _CONTEXTS[token] = context


def _take_context(token: str, session_id: str) -> _RewriteContext:
    with _CONTEXT_LOCK:
        _sweep_contexts_locked(time.monotonic())
        context = _CONTEXTS.get(token)
        if context is None or context.session_id != session_id:
            raise RewriteError("CONTEXT_EXPIRED", "rewrite context is missing or expired")
        _CONTEXTS.pop(token, None)
        return context


def _validate_structured_units(
    structured_result: object,
    expected_units: tuple[_SelectedUnitRange, ...],
) -> dict[str, str]:
    def invalid_slot(slot: object, expected_slot: _SelectedSlotRange) -> bool:
        return (
            not isinstance(slot, dict)
            or set(slot) != {"slot_id", "text"}
            or slot.get("slot_id") != expected_slot.slot_id
            or not isinstance(slot.get("text"), str)
        )

    if (
        not isinstance(structured_result, dict)
        or set(structured_result) != {"units", "facts_added"}
        or structured_result.get("facts_added") is not False
    ):
        raise RewriteError("MODEL_OUTPUT_INVALID", "facts_added must be false")
    units = structured_result.get("units")
    if not isinstance(units, list) or len(units) != len(expected_units):
        raise RewriteError("MODEL_OUTPUT_INVALID", "unit IDs must match prepared units exactly")
    total = 0
    normalized: dict[str, str] = {}
    for unit, expected_unit in zip(units, expected_units):
        if (
            not isinstance(unit, dict)
            or set(unit) != {"unit_id", "slots"}
            or unit.get("unit_id") != expected_unit.unit_id
        ):
            raise RewriteError("MODEL_OUTPUT_INVALID", "unit IDs must match prepared units exactly")
        slots = unit.get("slots")
        if not isinstance(slots, list) or len(slots) != len(expected_unit.slots):
            raise RewriteError("MODEL_OUTPUT_INVALID", "slot IDs must match prepared slots exactly")
        for slot, expected_slot in zip(slots, expected_unit.slots):
            if invalid_slot(slot, expected_slot):
                raise RewriteError("MODEL_OUTPUT_INVALID", "slot IDs must match prepared slots exactly")
            text = slot["text"]
            try:
                text_size = len(text.encode("utf-8"))
            except UnicodeEncodeError as exc:
                raise RewriteError(
                    "MODEL_OUTPUT_INVALID", "slot text is not valid UTF-8"
                ) from exc
            if FORBIDDEN_OUTPUT_RE.search(text):
                raise RewriteError("MODEL_OUTPUT_INVALID", "slot text contains forbidden syntax")
            total += text_size
            if total > 24_000:
                raise RewriteError("MODEL_OUTPUT_INVALID", "rewrite output is too large")
            normalized[expected_slot.slot_id] = text
    if not any(text.strip() for text in normalized.values()):
        raise RewriteError("MODEL_OUTPUT_INVALID", "rewrite output must not be empty")
    return normalized


def _topology_parts(signature: tuple[object, ...]) -> tuple[tuple[object, ...], tuple[object, ...]]:
    blocks = tuple(item[:4] for item in signature)
    protected = tuple(item[4:] for item in signature)
    return blocks, protected


def _heading_id(text: str) -> str:
    normalized = re.sub(r"\s+", "-", text.strip().casefold())
    normalized = "".join(
        character
        for character in normalized
        if character.isalnum() or character in {"-", "_"}
    )
    return re.sub(r"-+", "-", normalized).strip("-")


def _repair_internal_heading_anchor(
    original_map: MarkdownRewriteMap,
    child_markdown: str,
    selected_unit_ids: set[str],
) -> tuple[str, bool]:
    child_map = build_rewrite_map(child_markdown)
    if len(child_map.units) != len(original_map.units) or any(
        original.unit_type != child.unit_type
        for original, child in zip(original_map.units, child_map.units)
    ):
        raise RewriteError(
            "STRUCTURE_CONFLICT", "rewrite changed Markdown unit mapping"
        )
    selected_indexes = {
        index
        for index, unit in enumerate(original_map.units)
        if unit.unit_id in selected_unit_ids
    }
    original_anchor_index = build_document_anchor_index(original_map.source)
    child_anchor_index = build_document_anchor_index(child_markdown)
    original_heading_ids = [
        _heading_id(heading.text) for heading in original_anchor_index.headings
    ]
    child_heading_ids = [
        _heading_id(heading.text) for heading in child_anchor_index.headings
    ]
    replacements: list[tuple[int, int, bytes]] = []
    repaired_pairs: list[tuple[str, str]] = []
    for index in selected_indexes:
        original_unit = original_map.units[index]
        child_unit = child_map.units[index]
        if original_unit.unit_type != "heading" or child_unit.unit_type != "heading":
            continue
        old_id = _heading_id(_full_unit_visible_text(original_unit))
        new_id = _heading_id(_full_unit_visible_text(child_unit))
        if not old_id or old_id == new_id:
            continue
        if not new_id:
            if old_id in child_anchor_index.parsed_targets:
                raise RewriteError(
                    "FORMAT_CONFLICT", "linked heading ID cannot become empty"
                )
            continue
        ambiguous_anchor_index = (
            original_anchor_index.ambiguous or child_anchor_index.ambiguous
        )
        unique_heading_pair = (
            original_heading_ids.count(old_id) == 1
            and child_heading_ids.count(new_id) == 1
        )
        if ambiguous_anchor_index or not unique_heading_pair:
            raise RewriteError(
                "FORMAT_CONFLICT", "same-document heading anchor is ambiguous"
            )
        links = [
            link for link in child_anchor_index.links if link.target == old_id
        ]
        if not links:
            continue
        if (
            len(links) != 1
            or any(link.target == new_id for link in child_anchor_index.links)
        ):
            raise RewriteError(
                "FORMAT_CONFLICT", "same-document heading anchor is ambiguous"
            )
        link = links[0]
        encoded = "%" in link.source
        replacement = "#" + (
            quote(new_id, safe="-._~") if encoded else new_id
        )
        replacements.append(
            (link.start_byte, link.end_byte, replacement.encode("utf-8"))
        )
        repaired_pairs.append((old_id, new_id))
    if not replacements:
        return child_markdown, False
    child_bytes = child_markdown.encode("utf-8")
    for start, end, replacement in sorted(replacements, reverse=True):
        child_bytes = child_bytes[:start] + replacement + child_bytes[end:]
    repaired = child_bytes.decode("utf-8")
    repaired_index = build_document_anchor_index(repaired)
    repaired_heading_ids = [
        _heading_id(heading.text) for heading in repaired_index.headings
    ]
    if (
        repaired_index.ambiguous
        or any(
            any(link.target == old_id for link in repaired_index.links)
            or sum(link.target == new_id for link in repaired_index.links) != 1
            or repaired_heading_ids.count(new_id) != 1
            for old_id, new_id in repaired_pairs
        )
    ):
        raise RewriteError("FORMAT_CONFLICT", "repaired heading anchor is ambiguous")
    return repaired, True


def _protected_topology_matches(
    original_map: MarkdownRewriteMap,
    child_map: MarkdownRewriteMap,
    *,
    allow_link_destination_change: bool,
) -> bool:
    original = structure_signature(original_map)
    child = structure_signature(child_map)
    if len(original) != len(child):
        return False
    for original_unit, child_unit in zip(original, child):
        if original_unit[4] != child_unit[4]:
            return False
        original_anchors = original_unit[5]
        child_anchors = child_unit[5]
        if len(original_anchors) != len(child_anchors):
            return False
        for original_anchor, child_anchor in zip(original_anchors, child_anchors):
            if original_anchor[0] != child_anchor[0]:
                return False
            if (
                original_anchor != child_anchor
                and not (
                    allow_link_destination_change
                    and original_anchor[0] == "link_destination"
                )
            ):
                return False
    return True


def _current_highlight_ranges(
    original_map: MarkdownRewriteMap,
    child_map: MarkdownRewriteMap,
    context: _RewriteContext,
    slot_texts: dict[str, str],
) -> list[dict[str, int | str]]:
    selected_by_id = {unit.unit_id: unit for unit in context.selected_units}
    has_rewrite_result = False
    for original_unit in original_map.units:
        selected_unit = selected_by_id.get(original_unit.unit_id)
        if selected_unit is None:
            continue
        selected_slots = {slot.slot_id: slot for slot in selected_unit.slots}
        for original_slot in original_unit.slots:
            selected_slot = selected_slots.get(original_slot.slot_id)
            if selected_slot is None:
                continue
            visible_start = original_slot.visible_boundary_to_byte.index(
                selected_slot.start_byte
            )
            visible_end = original_slot.visible_boundary_to_byte.index(
                selected_slot.end_byte
            )
            if (
                slot_texts[selected_slot.slot_id]
                != original_slot.text[visible_start:visible_end]
            ):
                has_rewrite_result = True
                break
        if has_rewrite_result:
            break
    if not has_rewrite_result:
        return []

    ranges: list[tuple[int, int, str]] = []
    for index, original_unit in enumerate(original_map.units):
        selected_unit = selected_by_id.get(original_unit.unit_id)
        if selected_unit is None:
            continue
        child_unit = child_map.units[index]
        selected_slots = {slot.slot_id: slot for slot in selected_unit.slots}
        flat_cursor = 0
        highlight_spans: list[tuple[int, int]] = []
        for original_slot in original_unit.slots:
            selected_slot = selected_slots.get(original_slot.slot_id)
            if selected_slot is None:
                flat_cursor += len(original_slot.text)
                continue
            visible_start = original_slot.visible_boundary_to_byte.index(
                selected_slot.start_byte
            )
            visible_end = original_slot.visible_boundary_to_byte.index(
                selected_slot.end_byte
            )
            replacement = slot_texts[selected_slot.slot_id]
            highlight_start = flat_cursor + visible_start
            if replacement:
                highlight_spans.append(
                    (highlight_start, highlight_start + len(replacement))
                )
            flat_cursor += (
                visible_start
                + len(replacement)
                + len(original_slot.text)
                - visible_end
            )
        if flat_cursor != sum(len(slot.text) for slot in child_unit.slots):
            raise RewriteError(
                "STRUCTURE_CONFLICT", "rewritten slot highlight cannot be mapped"
            )
        child_cursor = 0
        for child_slot in child_unit.slots:
            child_end = child_cursor + len(child_slot.text)
            for highlight_start, highlight_end in highlight_spans:
                start = max(highlight_start, child_cursor)
                end = min(highlight_end, child_end)
                if start < end:
                    visible_ranges = visible_slot_byte_ranges(
                        child_map.source,
                        child_slot,
                        start - child_cursor,
                        end - child_cursor,
                    )
                    for range_start, range_end in visible_ranges:
                        ranges.append((range_start, range_end, child_unit.unit_type))
            child_cursor = child_end
    ranges.sort()
    merged: list[tuple[int, int, str]] = []
    for start, end, unit_type in ranges:
        if merged and start < merged[-1][1]:
            raise RewriteError("STRUCTURE_CONFLICT", "rewrite highlights overlap")
        if merged and start == merged[-1][1] and unit_type == merged[-1][2]:
            merged[-1] = (merged[-1][0], end, unit_type)
        else:
            merged.append((start, end, unit_type))
    if len(merged) > MAX_HIGHLIGHT_RANGES:
        return []
    return [
        {"start_byte": start, "end_byte": end, "unit_type": unit_type}
        for start, end, unit_type in merged
    ]


def _validate_affected_citations(
    context: _RewriteContext,
    provenance: dict,
    original_map: MarkdownRewriteMap,
    child_map: MarkdownRewriteMap,
) -> list[str]:
    citations = _load_final_result_citations(
        context.report_path, provenance, context.workspace_root
    )
    citation_occurrences = _citation_occurrence_index(original_map.source, citations)
    selected_ids = {unit.unit_id for unit in context.selected_units}
    affected_indexes = [
        index
        for index, unit in enumerate(original_map.units)
        if unit.unit_id in selected_ids
    ]

    def identities(rewrite_map: MarkdownRewriteMap) -> list[tuple[str, str]]:
        result = []
        for index in affected_indexes:
            for anchor in rewrite_map.units[index].protected:
                if anchor.kind != "citation":
                    continue
                match = CITATION_RE.fullmatch(anchor.source)
                if match is None:
                    raise RewriteError("FORMAT_CONFLICT", "citation syntax changed")
                result.append((match.group("index"), match.group("url")))
        return result

    expected = identities(original_map)
    if identities(child_map) != expected:
        raise RewriteError("FORMAT_CONFLICT", "citation identity or order changed")
    source_ids = []
    for index in affected_indexes:
        for anchor in original_map.units[index].protected:
            if anchor.kind != "citation":
                continue
            item = citation_occurrences.get((anchor.start_byte, anchor.end_byte))
            if item is None:
                raise RewriteError(
                    "FORMAT_CONFLICT", "citation is outside the final-result whitelist"
                )
            source_ids.append(str(item.get("id")))
    return source_ids


def _allows_empty_wrapper_deletion(
    original_map: MarkdownRewriteMap,
    context: _RewriteContext,
    slot_texts: dict[str, str],
    selected_ranges: dict[str, tuple[int, int]],
) -> bool:
    slots = {
        slot.slot_id: slot
        for unit in original_map.units
        for slot in unit.slots
    }
    empty_wrappers = set()
    for slot_id, text in slot_texts.items():
        if text or slot_id not in slots:
            continue
        slot = slots[slot_id]
        selected_whole_slot = selected_ranges[slot_id] == (
            slot.start_byte,
            slot.end_byte,
        )
        simple_inline_formats = (
            bool(slot.formats) and set(slot.formats) <= {"strong", "emphasis"}
        )
        if selected_whole_slot and simple_inline_formats:
            empty_wrappers.add(slot_id)
    if not empty_wrappers:
        return False
    placeholders = dict(slot_texts)
    for slot_id in empty_wrappers:
        placeholders[slot_id] = "x"
    try:
        placeholder_markdown = reconstruct_markdown(
            original_map, placeholders, selected_ranges=selected_ranges
        )
    except RewriteMapError:
        return False
    return structure_signature(build_rewrite_map(placeholder_markdown)) == (
        context.structure_signature
    )


def _versioning_error() -> RewriteError:
    return RewriteError("INVALID_PROVENANCE", _VERSIONING_ERROR_MESSAGE)


@dataclass(slots=True)
class _AuthorizedArtifactDirectory:
    root_path: Path
    root_descriptor: int | None
    directory_descriptor: int | None
    relative_parts: tuple[str, ...]
    directory_identity: tuple[int, int]
    fallback_chain: tuple[tuple[Path, tuple[int, int]], ...]


def _directory_open_flags() -> int:
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    nofollow = getattr(os, "O_NOFOLLOW", None)
    return flags | nofollow if nofollow is not None else flags


def _secure_parent_dirfd_supported() -> bool:
    return (
        os.open in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_dir_fd", set())
        and os.stat in getattr(os, "supports_follow_symlinks", set())
        and os.listdir in getattr(os, "supports_fd", set())
        and os.rename in getattr(os, "supports_dir_fd", set())
        and os.link in getattr(os, "supports_dir_fd", set())
        and os.unlink in getattr(os, "supports_dir_fd", set())
    )


def _opened_directory_identity(metadata: os.stat_result) -> tuple[int, int]:
    if not is_direct_directory(metadata):
        raise OSError("artifact parent must be a direct directory")
    return metadata.st_dev, metadata.st_ino


@contextmanager
def _authorized_artifact_directory(
    workspace_root: Path, directory: Path
) -> Iterator[_AuthorizedArtifactDirectory]:
    trusted_root = _lexical_workspace_root(workspace_root)
    lexical_directory = _lexical_absolute(directory)
    if not _inside(lexical_directory, trusted_root):
        raise OSError("artifact parent is outside the trusted workspace")
    relative_parts = lexical_directory.relative_to(trusted_root).parts
    root_named = os.lstat(trusted_root)
    root_identity = _opened_directory_identity(root_named)
    if not _secure_parent_dirfd_supported():
        chain: list[tuple[Path, tuple[int, int]]] = []
        current = trusted_root
        chain.append((current, root_identity))
        for component in relative_parts:
            current = current / component
            metadata = os.lstat(current)
            chain.append((current, _opened_directory_identity(metadata)))
        guard = _AuthorizedArtifactDirectory(
            trusted_root,
            None,
            None,
            tuple(relative_parts),
            chain[-1][1],
            tuple(chain),
        )
        yield guard
        return

    flags = _directory_open_flags()
    root_descriptor = os.open(trusted_root, flags, mode=0o700)
    directory_descriptor: int | None = None
    current_descriptor = os.dup(root_descriptor)
    try:
        if (
            _opened_directory_identity(os.fstat(root_descriptor))
            != root_identity
        ):
            raise OSError("trusted workspace root changed during open")
        for component in relative_parts:
            named = os.stat(
                component,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            _opened_directory_identity(named)
            opened_descriptor = os.open(
                component,
                flags,
                dir_fd=current_descriptor,
            )
            opened = os.fstat(opened_descriptor)
            if (
                _opened_directory_identity(named)
                != _opened_directory_identity(opened)
            ):
                os.close(opened_descriptor)
                raise OSError("artifact parent changed during traversal")
            os.close(current_descriptor)
            current_descriptor = opened_descriptor
        directory_descriptor = current_descriptor
        current_descriptor = -1
        directory_identity = _opened_directory_identity(
            os.fstat(directory_descriptor)
        )
        yield _AuthorizedArtifactDirectory(
            trusted_root,
            root_descriptor,
            directory_descriptor,
            tuple(relative_parts),
            directory_identity,
            (),
        )
    finally:
        if current_descriptor >= 0:
            os.close(current_descriptor)
        if directory_descriptor is not None:
            os.close(directory_descriptor)
        os.close(root_descriptor)


def _validate_authorized_artifact_directory(
    guard: _AuthorizedArtifactDirectory,
) -> None:
    if guard.directory_descriptor is None:
        for path, expected in guard.fallback_chain:
            metadata = os.lstat(path)
            if _opened_directory_identity(metadata) != expected:
                raise OSError("artifact parent changed during publication")
        return

    if __debug__ and guard.root_descriptor is None:
        raise AssertionError()
    named_root = os.lstat(guard.root_path)
    if (
        _opened_directory_identity(named_root)
        != _opened_directory_identity(os.fstat(guard.root_descriptor))
    ):
        raise OSError("trusted workspace root changed during publication")
    current_descriptor = os.dup(guard.root_descriptor)
    try:
        for component in guard.relative_parts:
            named = os.stat(
                component,
                dir_fd=current_descriptor,
                follow_symlinks=False,
            )
            _opened_directory_identity(named)
            opened_descriptor = os.open(
                component,
                _directory_open_flags(),
                dir_fd=current_descriptor,
            )
            opened = os.fstat(opened_descriptor)
            if (
                _opened_directory_identity(named)
                != _opened_directory_identity(opened)
            ):
                os.close(opened_descriptor)
                raise OSError("artifact parent changed during validation")
            os.close(current_descriptor)
            current_descriptor = opened_descriptor
        if (
            _opened_directory_identity(os.fstat(current_descriptor))
            != guard.directory_identity
        ):
            raise OSError("artifact parent identity changed")
    finally:
        os.close(current_descriptor)


def _restore_quarantined_path(
    quarantine: Path,
    target: Path,
    *,
    directory_fd: int | None = None,
) -> None:
    try:
        if directory_fd is None:
            os.link(quarantine, target, follow_symlinks=False)
        else:
            os.link(
                quarantine.name,
                target.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
                follow_symlinks=False,
            )
    except OSError:
        return
    try:
        if directory_fd is None:
            os.unlink(quarantine)
        else:
            os.unlink(quarantine.name, dir_fd=directory_fd)
    except OSError:
        pass


def _cleanup_owned_publication(
    path: Path, descriptor: int, *, directory_fd: int | None = None
) -> None:
    """Quarantine a published path before proving it is still our inode."""
    quarantine = path.with_name(f".{path.name}.cleanup-{uuid.uuid4().hex}")
    try:
        if directory_fd is None:
            os.rename(path, quarantine)
        else:
            os.rename(
                path.name,
                quarantine.name,
                src_dir_fd=directory_fd,
                dst_dir_fd=directory_fd,
            )
    except OSError:
        return
    try:
        owned = os.fstat(descriptor)
        moved = (
            os.lstat(quarantine)
            if directory_fd is None
            else os.stat(
                quarantine.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        )
    except OSError:
        _restore_quarantined_path(
            quarantine, path, directory_fd=directory_fd
        )
        return
    if (owned.st_dev, owned.st_ino) != (moved.st_dev, moved.st_ino):
        _restore_quarantined_path(
            quarantine, path, directory_fd=directory_fd
        )
        return
    try:
        if directory_fd is None:
            os.unlink(quarantine)
        else:
            os.unlink(quarantine.name, dir_fd=directory_fd)
    except OSError:
        pass


def _open_no_follow(
    path: Path,
    flags: int,
    mode: int = 0o600,
    *,
    directory_fd: int | None = None,
) -> int:
    """Open ``path`` refusing to follow a symlink final component.

    Uses ``os.O_NOFOLLOW`` when available. On platforms where it is missing
    (notably Windows, where ``os.O_NOFOLLOW`` is ``None``), fall back to an
    ``os.lstat`` symlink check before a regular open. Windows requires
    administrator/developer-mode to create symlinks, so the TOCTOU window is
    acceptable there and far better than failing every publication.
    """
    nofollow = getattr(os, "O_NOFOLLOW", None)
    if nofollow is not None:
        if directory_fd is None:
            return os.open(path, flags | nofollow, mode)
        return os.open(
            path.name,
            flags | nofollow,
            mode,
            dir_fd=directory_fd,
        )
    target: str | Path = path if directory_fd is None else path.name
    try:
        existing = (
            os.lstat(path)
            if directory_fd is None
            else os.stat(
                path.name,
                dir_fd=directory_fd,
                follow_symlinks=False,
            )
        )
    except FileNotFoundError:
        existing = None
    if existing is not None and stat.S_ISLNK(existing.st_mode):
        raise OSError(f"refusing to open symlink at {path}")
    if directory_fd is None:
        return os.open(target, flags, mode)
    return os.open(target, flags, mode, dir_fd=directory_fd)


def _publish_create(
    path: Path, payload: bytes, *, directory_fd: int | None = None
) -> int:
    """Create and fsync one immutable path, returning an ownership descriptor."""
    # O_BINARY is REQUIRED on Windows: os.open defaults to lowio text mode
    # (unlike the built-in open()), so os.write translates "\n" -> "\r\n".
    # That would make the on-disk bytes differ from `payload`/the sha256 we
    # computed over `payload`, breaking content_sha256 integrity (highlights
    # dropped, prepare_html_export REVISION_CONFLICT) on Windows.
    publish_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
    if hasattr(os, "O_BINARY"):
        publish_flags |= os.O_BINARY
    try:
        descriptor = _open_no_follow(
            path,
            publish_flags,
            0o600,
            directory_fd=directory_fd,
        )
    except OSError as exc:
        logger.error(
            "PROVENANCE_DIAG _publish_create open failed type=%s errno=%s "
            "msg=%s path=%s nofollow=%s",
            type(exc).__name__,
            exc.errno,
            exc,
            path,
            getattr(os, "O_NOFOLLOW", None) is not None,
        )
        raise
    try:
        view = memoryview(payload)
        while view:
            written = os.write(descriptor, view)
            if written < 1:
                raise OSError("artifact publication made no progress")
            view = view[written:]
        os.fsync(descriptor)
    except BaseException:
        _cleanup_owned_publication(
            path, descriptor, directory_fd=directory_fd
        )
        os.close(descriptor)
        raise
    return descriptor


def _allocate_paths(
    parent_path: Path,
    parent_provenance: dict,
    parent_markdown: str,
    *,
    directory_fd: int | None = None,
) -> ArtifactPaths:
    try:
        return allocate_next_paths(
            parent_path,
            parent_provenance,
            parent_markdown,
            directory_fd=directory_fd,
        )
    except (
        ArtifactNamingError,
        OSError,
        json.JSONDecodeError,
        UnicodeError,
    ) as exc:
        logger.error(
            "PROVENANCE_DIAG _allocate_paths failed type=%s code=%s msg=%s parent=%s",
            type(exc).__name__,
            getattr(exc, "code", None),
            exc,
            parent_path,
        )
        raise _versioning_error() from exc


def _publish_child(
    *,
    workspace_root: Path,
    parent_path: Path,
    parent_provenance: dict,
    parent_markdown: str,
    child_markdown: bytes,
    child_provenance: dict,
) -> tuple[Path, Path, dict]:
    try:
        authorization = _authorized_artifact_directory(
            workspace_root, parent_path.parent
        )
        with authorization as authorized:
            return _publish_child_in_authorized_directory(
                authorized=authorized,
                parent_path=parent_path,
                parent_provenance=parent_provenance,
                parent_markdown=parent_markdown,
                child_markdown=child_markdown,
                child_provenance=child_provenance,
            )
    except RewriteError:
        raise
    except (OSError, RuntimeError, ValueError) as exc:
        raise _versioning_error() from exc


def _publish_child_in_authorized_directory(
    *,
    authorized: _AuthorizedArtifactDirectory,
    parent_path: Path,
    parent_provenance: dict,
    parent_markdown: str,
    child_markdown: bytes,
    child_provenance: dict,
) -> tuple[Path, Path, dict]:
    directory_fd = authorized.directory_descriptor
    for attempt in range(MAX_PUBLICATION_ATTEMPTS):
        _validate_authorized_artifact_directory(authorized)
        paths = _allocate_paths(
            parent_path,
            parent_provenance,
            parent_markdown,
            directory_fd=directory_fd,
        )
        _validate_authorized_artifact_directory(authorized)
        published_provenance = dict(child_provenance)
        published_provenance.update(
            {
                "markdown_path": str(paths.markdown_path),
                "version_number": paths.version.version_number,
                "version_base_stem": paths.version.base_stem,
            }
        )
        try:
            provenance_payload = json.dumps(
                published_provenance, ensure_ascii=False, indent=2
            ).encode("utf-8")
        except (TypeError, ValueError) as exc:
            logger.error(
                "PROVENANCE_DIAG _publish_child json-encode failed type=%s msg=%s",
                type(exc).__name__,
                exc,
            )
            raise _versioning_error() from exc

        provenance_descriptor: int | None = None
        try:
            provenance_descriptor = _publish_create(
                paths.provenance_path,
                provenance_payload,
                directory_fd=directory_fd,
            )
            _validate_authorized_artifact_directory(authorized)
        except FileExistsError:
            logger.info(
                "PROVENANCE_DIAG _publish_child provenance exists attempt=%s path=%s",
                attempt,
                paths.provenance_path,
            )
            continue
        except OSError as exc:
            if provenance_descriptor is not None:
                _cleanup_owned_publication(
                    paths.provenance_path,
                    provenance_descriptor,
                    directory_fd=directory_fd,
                )
                os.close(provenance_descriptor)
                provenance_descriptor = None
            logger.error(
                "PROVENANCE_DIAG _publish_child provenance create failed type=%s "
                "errno=%s msg=%s path=%s",
                type(exc).__name__,
                exc.errno,
                exc,
                paths.provenance_path,
            )
            raise _versioning_error() from exc
        if __debug__ and provenance_descriptor is None:
            raise AssertionError()
        markdown_descriptor: int | None = None
        try:
            try:
                # Provenance is staged first, but Markdown is the commit marker:
                # a sidecar without its Markdown peer is never a committed report.
                _validate_authorized_artifact_directory(authorized)
                markdown_descriptor = _publish_create(
                    paths.markdown_path,
                    child_markdown,
                    directory_fd=directory_fd,
                )
                _validate_authorized_artifact_directory(authorized)
            except FileExistsError:
                _cleanup_owned_publication(
                    paths.provenance_path,
                    provenance_descriptor,
                    directory_fd=directory_fd,
                )
                logger.info(
                    "PROVENANCE_DIAG _publish_child markdown exists attempt=%s path=%s",
                    attempt,
                    paths.markdown_path,
                )
                continue
            except OSError as exc:
                if markdown_descriptor is not None:
                    _cleanup_owned_publication(
                        paths.markdown_path,
                        markdown_descriptor,
                        directory_fd=directory_fd,
                    )
                    os.close(markdown_descriptor)
                    markdown_descriptor = None
                _cleanup_owned_publication(
                    paths.provenance_path,
                    provenance_descriptor,
                    directory_fd=directory_fd,
                )
                logger.error(
                    "PROVENANCE_DIAG _publish_child markdown create failed type=%s "
                    "errno=%s msg=%s path=%s",
                    type(exc).__name__,
                    exc.errno,
                    exc,
                    paths.markdown_path,
                )
                raise _versioning_error() from exc
            except BaseException:
                if markdown_descriptor is not None:
                    _cleanup_owned_publication(
                        paths.markdown_path,
                        markdown_descriptor,
                        directory_fd=directory_fd,
                    )
                    os.close(markdown_descriptor)
                    markdown_descriptor = None
                _cleanup_owned_publication(
                    paths.provenance_path,
                    provenance_descriptor,
                    directory_fd=directory_fd,
                )
                raise
            else:
                if __debug__ and markdown_descriptor is None:
                    raise AssertionError()
                os.close(markdown_descriptor)
                markdown_descriptor = None
                return (
                    paths.markdown_path,
                    paths.provenance_path,
                    published_provenance,
                )
        finally:
            if provenance_descriptor is not None:
                os.close(provenance_descriptor)
    logger.error(
        "PROVENANCE_DIAG _publish_child exhausted attempts=%s parent=%s",
        MAX_PUBLICATION_ATTEMPTS,
        parent_path,
    )
    raise _versioning_error()


def _atomic_write(path: Path, payload: bytes) -> None:
    fd, temp_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temp_name, path)
    finally:
        try:
            os.unlink(temp_name)
        except FileNotFoundError:
            pass


def _first_visible_character_after(
    rewrite_map: MarkdownRewriteMap, slot_id: str, byte_offset: int
) -> tuple[int, str] | None:
    unit = next(
        (
            unit
            for unit in rewrite_map.units
            if any(slot.slot_id == slot_id for slot in unit.slots)
        ),
        None,
    )
    if unit is None:
        return None
    candidates: list[tuple[int, str]] = []
    for slot in unit.slots:
        visible_index = next(
            (
                index
                for index in range(len(slot.text))
                if slot.visible_boundary_to_byte[index] >= byte_offset
            ),
            None,
        )
        if visible_index is None:
            continue
        ranges = visible_slot_byte_ranges(
            rewrite_map.source, slot, visible_index, visible_index + 1
        )
        if ranges:
            candidates.append((ranges[0][0], slot.text[visible_index]))
    for anchor in unit.protected:
        visible_text = _anchor_visible_text(anchor)
        if anchor.start_byte >= byte_offset and visible_text:
            candidates.append((anchor.start_byte, visible_text[0]))
    if not candidates:
        return None
    candidate = min(candidates)
    gap = rewrite_map.source.encode("utf-8")[byte_offset:candidate[0]]
    return None if b"\r" in gap or b"\n" in gap else candidate


def _normalize_unselected_right_punctuation(
    original_map: MarkdownRewriteMap,
    context: _RewriteContext,
    slot_texts: dict[str, str],
) -> dict[str, str]:
    source_bytes = original_map.source.encode("utf-8")
    selection = source_bytes[
        context.selection_start_byte:context.selection_end_byte
    ].decode("utf-8")
    selected_slots = [
        slot
        for unit in context.selected_units
        for slot in unit.slots
    ]
    if not selected_slots or selection[-1:] in _BOUNDARY_PUNCTUATION:
        return slot_texts
    rightmost_slot = max(selected_slots, key=lambda slot: slot.end_byte)
    if rightmost_slot.end_byte != context.selection_end_byte:
        return slot_texts
    right_visible = _first_visible_character_after(
        original_map, rightmost_slot.slot_id, context.selection_end_byte
    )
    if right_visible is None or right_visible[1] not in _BOUNDARY_PUNCTUATION:
        return slot_texts
    replacement = slot_texts[rightmost_slot.slot_id]
    replacement_core = replacement.rstrip()
    if (
        not replacement_core
        or replacement_core[-1] not in _BOUNDARY_PUNCTUATION
    ):
        return slot_texts
    replacement_core = replacement_core.rstrip(_BOUNDARY_PUNCTUATION)
    if not replacement_core.strip():
        raise RewriteError(
            "MODEL_OUTPUT_INVALID",
            "rewrite output must not become empty after punctuation normalization",
        )
    normalized = dict(slot_texts)
    normalized[rightmost_slot.slot_id] = replacement_core
    return normalized


def commit_rewrite(*, context_token: str, session_id: str, structured_result: object) -> dict:
    context = _take_context(context_token, session_id)
    slot_texts = _validate_structured_units(structured_result, context.selected_units)
    document_id = context.document_id
    with _document_lock(document_id):
        try:
            provenance, provenance_sha256 = _load_provenance(
                context.report_path, context.workspace_root
            )
        except RewriteError as exc:
            raise RewriteError("REVISION_CONFLICT", "the report provenance changed") from exc
        provenance_identity = (
            provenance.get("document_id"),
            provenance.get("revision_id"),
            provenance.get("content_sha256"),
            provenance.get("final_result_path"),
            provenance.get("final_result_sha256"),
        )
        expected_identity = (
            context.document_id,
            context.parent_revision_id,
            context.parent_hash,
            context.final_result_path,
            context.final_result_sha256,
        )
        if provenance_sha256 != context.provenance_sha256 or provenance_identity != expected_identity:
            raise RewriteError("REVISION_CONFLICT", "the report provenance changed")
        try:
            current_bytes = _read_regular_bounded_beneath(
                context.report_path,
                context.workspace_root,
                MARKDOWN_MAX_BYTES,
                "report markdown",
            )
        except (OSError, TypeError, ValueError) as exc:
            raise RewriteError(
                "REVISION_CONFLICT", "the parent report changed"
            ) from exc
        parent_hash = _sha256(current_bytes)
        if parent_hash != context.parent_hash:
            raise RewriteError("REVISION_CONFLICT", "the parent report changed")
        current_markdown = current_bytes.decode("utf-8")
        original_map = build_rewrite_map(current_markdown)
        slot_texts = _normalize_unselected_right_punctuation(
            original_map, context, slot_texts
        )
        selected_ranges = {
            slot.slot_id: (slot.start_byte, slot.end_byte)
            for unit in context.selected_units
            for slot in unit.slots
        }
        try:
            child_markdown = reconstruct_markdown(
                original_map, slot_texts, selected_ranges=selected_ranges
            )
        except RewriteMapError as exc:
            raise RewriteError("STRUCTURE_CONFLICT", str(exc)) from exc
        child_markdown, anchor_repaired = _repair_internal_heading_anchor(
            original_map,
            child_markdown,
            {unit.unit_id for unit in context.selected_units},
        )
        child_map = build_rewrite_map(child_markdown)
        if tuple(region.kind for region in child_map.unsupported_regions) != tuple(
            region.kind for region in original_map.unsupported_regions
        ):
            raise RewriteError(
                "STRUCTURE_CONFLICT", "rewrite changed unsupported Markdown topology"
            )
        expected_blocks, expected_protected = _topology_parts(context.structure_signature)
        child_blocks, child_protected = _topology_parts(structure_signature(child_map))
        if child_blocks != expected_blocks:
            raise RewriteError("STRUCTURE_CONFLICT", "rewrite changed Markdown block topology")
        protected_matches = child_protected == expected_protected or (
            anchor_repaired
            and _protected_topology_matches(
                original_map,
                child_map,
                allow_link_destination_change=True,
            )
        )
        if not protected_matches and not _allows_empty_wrapper_deletion(
            original_map, context, slot_texts, selected_ranges
        ):
            raise RewriteError("FORMAT_CONFLICT", "rewrite changed protected inline topology")
        citation_ids = _validate_affected_citations(
            context, provenance, original_map, child_map
        )
        result_bytes = "\n".join(slot_texts.values()).encode("utf-8")
        revision_id = f"rev_{uuid.uuid4().hex}"
        child_bytes = child_markdown.encode("utf-8")
        child_hash = _sha256(child_bytes)
        child_provenance = dict(provenance)
        history = list(provenance.get("rewrite_history") or [])
        history.append({
            "rewrite_protocol_version": 2,
            "action": context.action,
            "parent_revision_id": context.parent_revision_id,
            "selection_sha256": _sha256(
                current_bytes[context.selection_start_byte:context.selection_end_byte]
            ),
            "result_sha256": _sha256(result_bytes),
            "unit_types": [
                unit.unit_type
                for unit in original_map.units
                if unit.unit_id in {item.unit_id for item in context.selected_units}
            ],
            "citation_ids": citation_ids,
        })
        highlights = _current_highlight_ranges(
            original_map, child_map, context, slot_texts
        )
        child_provenance.update({
            "rewrite_protocol_version": 2,
            "revision_id": revision_id,
            "parent_revision_id": context.parent_revision_id,
            "content_sha256": child_hash,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "operation": {"action": context.action},
            "rewrite_history": history,
            "rewrite_highlights": {
                "revision_id": revision_id,
                "offset_unit": "utf8_byte",
                "ranges": highlights,
            },
        })
        child_path, provenance_path, child_provenance = _publish_child(
            workspace_root=context.workspace_root,
            parent_path=context.report_path,
            parent_provenance=provenance,
            parent_markdown=current_markdown,
            child_markdown=child_bytes,
            child_provenance=child_provenance,
        )
    return {
        "document_id": document_id,
        "revision_id": revision_id,
        "parent_revision_id": context.parent_revision_id,
        "report_path": str(child_path),
        "provenance_path": str(provenance_path),
        "citation_integrity_status": "verified",
        "citation_semantic_status": "not_verified",
    }
