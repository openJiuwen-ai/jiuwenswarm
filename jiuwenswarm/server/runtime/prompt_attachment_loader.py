# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Prompt attachment directory loader for jiuwenswarm.

This module is the jiuwenswarm-side file hot-load adapter. Before model calls,
it reads ``.md``/``.txt`` files from the current session's prompt attachment
directory, converts them into agent-core ``PromptAttachment`` objects, and
syncs them into the agent's ``PromptAttachmentManager``.
"""
from __future__ import annotations

import hashlib
import logging
import os
import re
import stat
import threading
import time
from pathlib import Path
from typing import Any, Iterable

from openjiuwen.harness.prompts.prompt_attachment_manager import (
    PromptAttachment,
    PromptAttachmentKind,
)


logger = logging.getLogger(__name__)

SESSION_SOURCE = "jiuwenswarm.prompt_attachment.session"
DEFAULT_MAX_FILE_CHARS = 12000
_SAFE_SESSION_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")
_TEXT_SUFFIXES = frozenset({".md", ".txt"})
# 进程内已 ensure 过的 root，避免每次 create_instance 重复读/写 README。
_LAYOUT_READY: set[str] = set()
_LAYOUT_READY_LOCK = threading.Lock()
_README_TEXT = """# Prompt Attachment

Files in this directory are injected as dynamic prompt attachments for model calls.
They are not user-uploaded attachments and are not written to long-term
conversation history.

Layout:
- <session_id>/session/: hot-loaded prompt attachment files for one session.

Markdown frontmatter is intentionally small: simple key-value fields and one
level of metadata map are supported. Arrays, multiline strings, and full YAML
features are not parsed by this loader.
"""
_KIND_BY_STEM = {
    "runtime": PromptAttachmentKind.RUNTIME,
    "request_context": PromptAttachmentKind.RUNTIME,
    "diagnostics": PromptAttachmentKind.DIAGNOSTIC,
    "memory_summary": PromptAttachmentKind.MEMORY,
    "open_files": PromptAttachmentKind.FILE,
}
_USER_SOURCE = "jiuwenswarm.prompt_attachment.user"
_LOCKS_GUARD = threading.Lock()
_PATH_LOCKS: dict[str, threading.Lock] = {}


def _is_relative_to(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
        return True
    except ValueError:
        return False


def _is_reparse_path(path: Path) -> bool:
    """Return True for symlink, junction, or other Windows reparse-point paths."""

    if path.is_symlink():
        return True
    is_junction = getattr(path, "is_junction", None)
    if callable(is_junction) and is_junction():
        return True
    try:
        attrs = getattr(os.lstat(path), "st_file_attributes", 0)
    except OSError:
        return True
    return bool(attrs & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0))


def _iter_safe_files(session_dir: Path, suffixes: frozenset[str]) -> Iterable[Path]:
    if not session_dir.exists() or not session_dir.is_dir():
        return
    session_root = session_dir.resolve()
    pending = [session_dir]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir(), key=lambda item: item.as_posix())
        except OSError as exc:
            logger.warning("[PromptAttachmentLoader] failed to list prompt attachment directory %s: %s", current, exc)
            continue
        for path in entries:
            try:
                relative = path.relative_to(session_dir)
            except ValueError:
                continue
            if any(part.startswith(".") for part in relative.parts):
                continue
            if _is_reparse_path(path):
                logger.warning("[PromptAttachmentLoader] skip linked prompt attachment path: %s", path)
                continue
            try:
                resolved = path.resolve(strict=True)
            except OSError as exc:
                logger.warning("[PromptAttachmentLoader] failed to resolve prompt attachment path %s: %s", path, exc)
                continue
            if not _is_relative_to(resolved, session_root):
                logger.warning("[PromptAttachmentLoader] skip prompt attachment path outside session: %s", path)
                continue
            if path.is_dir():
                pending.append(path)
            elif path.is_file() and path.suffix.lower() in suffixes:
                yield path


def _metadata_with_origin_source(metadata: dict[str, Any], origin_source: str | None) -> dict[str, Any]:
    result = dict(metadata)
    if origin_source and origin_source != SESSION_SOURCE:
        result.setdefault("origin_source", origin_source)
    return result


def sanitize_session_id(session_id: str | None) -> str:
    """Return a deterministic path-safe session id."""

    raw = str(session_id or "").strip()
    if not raw:
        return "default"
    safe = _SAFE_SESSION_CHARS.sub("_", raw.replace("/", "_").replace("\\", "_")).strip("._-")
    if not safe:
        safe = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    if safe in {".", ".."}:
        safe = f"session_{hashlib.sha256(raw.encode('utf-8')).hexdigest()[:16]}"
    if len(safe) > 80:
        digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
        safe = f"{safe[:67]}_{digest}"
    return safe


def _safe_id_part(value: str) -> str:
    raw = str(value or "").strip()
    safe = _SAFE_SESSION_CHARS.sub("_", raw).strip("._-")
    if safe:
        return safe
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]


def _path_lock(path: Path) -> threading.Lock:
    key = str(path.resolve())
    with _LOCKS_GUARD:
        lock = _PATH_LOCKS.get(key)
        if lock is None:
            lock = threading.Lock()
            _PATH_LOCKS[key] = lock
        return lock


def _atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp.{os.getpid()}.{threading.get_ident()}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)


def _kind_value(kind: PromptAttachmentKind | str) -> str:
    return kind.value if isinstance(kind, PromptAttachmentKind) else str(kind)


def _parse_scalar(value: str) -> Any:
    stripped = value.strip()
    if stripped in {"true", "True"}:
        return True
    if stripped in {"false", "False"}:
        return False
    if stripped in {"null", "None", "~"}:
        return None
    try:
        return int(stripped)
    except ValueError:
        return stripped.strip('"').strip("'")


def _parse_frontmatter(raw: str) -> tuple[dict[str, Any], str]:
    if not raw.startswith("---\n") and not raw.startswith("---\r\n"):
        return {}, raw
    normalized = raw.replace("\r\n", "\n")
    end = normalized.find("\n---\n", 4)
    if end < 0:
        return {}, raw
    header = normalized[4:end]
    body = normalized[end + len("\n---\n"):]
    if body.startswith("\n"):
        body = body[1:]
    data: dict[str, Any] = {}
    current_map: dict[str, Any] | None = None
    for line in header.splitlines():
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith((" ", "\t")) and current_map is not None and ":" in line:
            key, value = line.strip().split(":", 1)
            current_map[key.strip()] = _parse_scalar(value)
            continue
        current_map = None
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        key = key.strip()
        if value.strip():
            data[key] = _parse_scalar(value)
        else:
            nested: dict[str, Any] = {}
            data[key] = nested
            current_map = nested
    return data, body


def _dump_frontmatter(data: dict[str, Any]) -> str:
    lines = ["---"]
    for key in sorted(data):
        value = data[key]
        if isinstance(value, dict):
            lines.append(f"{key}:")
            for nested_key in sorted(value):
                lines.append(f"  {nested_key}: {value[nested_key]}")
        else:
            lines.append(f"{key}: {value}")
    lines.append("---")
    return "\n".join(lines) + "\n\n"


def _resolve_from_context(ctx: Any, *names: str) -> str | None:
    for name in names:
        value = getattr(ctx, name, None)
        if value:
            return str(value)
    extra = getattr(ctx, "extra", None)
    if isinstance(extra, dict):
        for name in names:
            value = extra.get(name)
            if value:
                return str(value)
    request = getattr(ctx, "request", None)
    if request is not None:
        for name in names:
            value = getattr(request, name, None)
            if value:
                return str(value)
    session = getattr(ctx, "session", None)
    if session is not None:
        for name in names:
            value = getattr(session, name, None)
            if value:
                return str(value)
    return None


class PromptAttachmentFileStore:
    """File CRUD helper for session prompt attachment directories."""

    def __init__(self, root: Path | str, *, max_file_chars: int = DEFAULT_MAX_FILE_CHARS) -> None:
        self.root = Path(root)
        self.max_file_chars = max_file_chars

    def bind_context(self, ctx: Any) -> "PromptAttachmentContextStore":
        return PromptAttachmentContextStore(self, ctx)

    def for_session(self, session_id: str) -> "PromptAttachmentSessionStore":
        return PromptAttachmentSessionStore(self, session_id=session_id)

    def add_markdown(
        self,
        *,
        session_id: str,
        content: str,
        name: str | None = None,
        section: str | None = None,
        priority: int = 100,
        kind: PromptAttachmentKind | str = PromptAttachmentKind.TEXT,
        source: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PromptAttachment:
        section_id = _safe_id_part(section) if section else self._section_from_name(name)
        path = self._file_path(session_id=session_id, name=name or section_id, suffix=".md")
        if path.exists():
            raise FileExistsError(f"prompt attachment already exists: {path}")
        frontmatter = self._frontmatter(
            section=section_id,
            priority=priority,
            kind=kind,
            source=source or _USER_SOURCE,
            metadata=metadata,
        )
        with _path_lock(path):
            _atomic_write_text(path, _dump_frontmatter(frontmatter) + content)
        return self._item_from_file(path, session_id=session_id)

    def update_markdown(
        self,
        id_or_name: str,
        *,
        session_id: str,
        content: str | None = None,
        priority: int | None = None,
        source: str | None = None,
        kind: PromptAttachmentKind | str | None = None,
        metadata: dict[str, Any] | None = None,
        metadata_replace: bool = False,
        replace: bool = False,
    ) -> PromptAttachment:
        path = self._resolve_id_or_name(id_or_name, session_id=session_id)
        if path is None:
            raise FileNotFoundError(f"prompt attachment does not exist: {id_or_name}")
        with _path_lock(path):
            old_meta, old_content = _parse_frontmatter(path.read_text(encoding="utf-8"))
            next_meta = {} if replace else dict(old_meta)
            if old_meta.get("section") is not None:
                next_meta["section"] = old_meta["section"]
            if priority is not None:
                next_meta["priority"] = priority
            if source is not None:
                next_meta["source"] = source
            if kind is not None:
                next_meta["kind"] = _kind_value(self._coerce_kind(kind))
            if metadata is not None:
                if metadata_replace:
                    next_meta["metadata"] = dict(metadata)
                else:
                    next_meta["metadata"] = {**dict(next_meta.get("metadata") or {}), **metadata}
            next_content = old_content if content is None else content
            _atomic_write_text(path, _dump_frontmatter(next_meta) + next_content)
        return self._item_from_file(path, session_id=session_id)

    def get(self, id_or_name: str, *, session_id: str) -> PromptAttachment | None:
        path = self._resolve_id_or_name(id_or_name, session_id=session_id)
        if path is None:
            return None
        return self._item_from_file(path, session_id=session_id)

    def delete(self, id_or_name: str, *, session_id: str) -> bool:
        path = self._resolve_id_or_name(id_or_name, session_id=session_id)
        if path is None:
            return False
        with _path_lock(path):
            if not path.exists():
                return False
            path.unlink()
        return True

    def list(self, *, session_id: str) -> list[PromptAttachment]:
        session_dir = self._session_dir(session_id)
        items: list[PromptAttachment] = []
        for path in _iter_safe_files(session_dir, _TEXT_SUFFIXES):
            item = self._item_from_file(path, session_id=session_id)
            if item is not None:
                items.append(item)
        return sorted(items, key=lambda item: (item.priority, item.source or "", item.section))

    def _item_from_file(self, path: Path, *, session_id: str) -> PromptAttachment | None:
        raw = self._read_text_file(path, self._session_dir(session_id))
        if raw is None:
            return None
        meta, content = _parse_frontmatter(raw)
        session_dir = self._session_dir(session_id)
        section = _safe_id_part(str(meta.get("section") or self.relative_key(path, session_dir)))
        metadata = _metadata_with_origin_source(dict(meta.get("metadata") or {}), meta.get("source"))
        metadata.update({"path": str(path), "relative_path": path.relative_to(session_dir).as_posix()})
        return PromptAttachment(
            id=self._item_id(session_id=session_id, section=section),
            section=section,
            kind=meta.get("kind") or PromptAttachmentLoader.kind_for_file(path),
            content=content,
            priority=int(meta.get("priority") or 100),
            source=SESSION_SOURCE,
            session_id=session_id,
            metadata=metadata,
            content_kind="text/markdown" if path.suffix.lower() == ".md" else "text/plain",
        )

    def _resolve_id_or_name(
        self,
        id_or_name: str,
        *,
        session_id: str,
    ) -> Path | None:
        session_dir = self._session_dir(session_id)
        for path in _iter_safe_files(session_dir, _TEXT_SUFFIXES):
            relative_key = self.relative_key(path, session_dir)
            generated_id = f"session.{sanitize_session_id(session_id)}.{relative_key}"
            if id_or_name in {path.name, path.stem, relative_key, generated_id}:
                return path
            item = self._item_from_file(path, session_id=session_id)
            if item is not None and item.id == id_or_name:
                return path
        return None

    def _file_path(self, *, session_id: str, name: str, suffix: str) -> Path:
        relative = self._safe_relative_file_name(name=name, suffix=suffix)
        return self._session_dir(session_id) / relative

    def _session_dir(self, session_id: str) -> Path:
        return self.root / sanitize_session_id(session_id) / "session"

    @staticmethod
    def _item_id(*, session_id: str, section: str) -> str:
        return f"session.{sanitize_session_id(session_id)}.{_safe_id_part(section)}"

    @staticmethod
    def _section_from_name(name: str | None) -> str:
        if not name:
            return _safe_id_part(f"attachment_{time.time_ns()}_{threading.get_ident()}")
        raw_path = Path(name)
        parts = list(raw_path.with_suffix("").parts)
        return ".".join(_safe_id_part(part) for part in parts)

    @staticmethod
    def _frontmatter(
        *,
        section: str,
        priority: int,
        kind: PromptAttachmentKind | str,
        source: str,
        metadata: dict[str, Any] | None,
    ) -> dict[str, Any]:
        data: dict[str, Any] = {
            "section": section,
            "priority": priority,
            "kind": _kind_value(kind),
            "source": source,
        }
        if metadata:
            data["metadata"] = dict(metadata)
        return data

    def _read_text_file(self, path: Path, session_dir: Path) -> str | None:
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            logger.debug(
                "[PromptAttachmentLoader] skip empty prompt attachment file: %s",
                path.relative_to(session_dir).as_posix(),
            )
            return None
        if self.max_file_chars > 0 and len(text) > self.max_file_chars:
            original_chars = len(text)
            text = text[:self.max_file_chars] + "\n\n[Prompt attachment file truncated by jiuwenswarm loader.]"
            logger.warning(
                "[PromptAttachmentLoader] truncated prompt attachment file: "
                "path=%s original_chars=%s truncated_chars=%s",
                path.relative_to(session_dir).as_posix(),
                original_chars,
                len(text),
            )
        return text

    @staticmethod
    def _coerce_kind(kind: PromptAttachmentKind | str) -> PromptAttachmentKind:
        return kind if isinstance(kind, PromptAttachmentKind) else PromptAttachmentKind(str(kind))

    @staticmethod
    def _safe_relative_file_name(*, name: str, suffix: str) -> Path:
        raw_name = str(name or "").strip()
        raw_path = Path(raw_name if Path(raw_name).suffix else f"{raw_name}{suffix}")
        if raw_path.is_absolute() or any(part in {"", ".", ".."} for part in raw_path.parts):
            raise ValueError(f"unsafe prompt attachment file name: {name}")
        if any(part.startswith(".") for part in raw_path.parts):
            raise ValueError(f"hidden prompt attachment file names are not supported: {name}")
        if raw_path.suffix.lower() not in _TEXT_SUFFIXES:
            raise ValueError(f"unsupported prompt attachment file suffix: {raw_path.suffix}")
        parts = []
        for index, part in enumerate(raw_path.parts):
            part_path = Path(part)
            if index == len(raw_path.parts) - 1:
                parts.append(f"{_safe_id_part(part_path.stem)}{part_path.suffix.lower()}")
            else:
                parts.append(_safe_id_part(part))
        return Path(*parts)

    @staticmethod
    def relative_key(path: Path, session_dir: Path) -> str:
        rel = path.relative_to(session_dir).with_suffix("")
        return ".".join(_safe_id_part(part) for part in rel.parts)


class PromptAttachmentContextStore:
    """Context-bound file writer that hides the session id from callers."""

    def __init__(self, store: PromptAttachmentFileStore, ctx: Any) -> None:
        self._store = store
        self.session_id = _resolve_from_context(ctx, "session_id", "_session_id", "id") or "default"

    def add_markdown(self, **kwargs: Any) -> PromptAttachment:
        return self._store.add_markdown(session_id=self.session_id, **kwargs)

    def update_markdown(self, id_or_name: str, **kwargs: Any) -> PromptAttachment:
        return self._store.update_markdown(id_or_name, session_id=self.session_id, **kwargs)

    def get(self, id_or_name: str) -> PromptAttachment | None:
        return self._store.get(id_or_name, session_id=self.session_id)

    def delete(self, id_or_name: str) -> bool:
        return self._store.delete(id_or_name, session_id=self.session_id)

    def list(self) -> list[PromptAttachment]:
        return self._store.list(session_id=self.session_id)


class PromptAttachmentSessionStore:
    """Session-bound writer for services that know session_id but not full ctx."""

    def __init__(self, store: PromptAttachmentFileStore, *, session_id: str) -> None:
        self._store = store
        self.session_id = session_id

    def add_markdown(self, **kwargs: Any) -> PromptAttachment:
        return self._store.add_markdown(session_id=self.session_id, **kwargs)

    def update_markdown(self, id_or_name: str, **kwargs: Any) -> PromptAttachment:
        return self._store.update_markdown(id_or_name, session_id=self.session_id, **kwargs)

    def get(self, id_or_name: str) -> PromptAttachment | None:
        return self._store.get(id_or_name, session_id=self.session_id)

    def delete(self, id_or_name: str) -> bool:
        return self._store.delete(id_or_name, session_id=self.session_id)

    def list(self) -> list[PromptAttachment]:
        return self._store.list(session_id=self.session_id)


class PromptAttachmentLoader:
    """Load jiuwenswarm prompt attachment files into a DeepAgent manager."""

    def __init__(self, root: Path | str, *, max_file_chars: int = DEFAULT_MAX_FILE_CHARS) -> None:
        self.root = Path(root)
        self.max_file_chars = max_file_chars
        self.file_store = PromptAttachmentFileStore(self.root, max_file_chars=max_file_chars)

    def bind_context(self, ctx: Any) -> PromptAttachmentContextStore:
        """Return a context-bound file writer facade."""

        return self.file_store.bind_context(ctx)

    def for_session(self, session_id: str) -> PromptAttachmentSessionStore:
        """Return a session-bound file writer facade."""

        return self.file_store.for_session(session_id)

    def ensure_layout(self) -> None:
        """Create the root prompt attachment layout（同步；带进程级跳过缓存）。"""
        root_key = str(self.root.resolve())
        with _LAYOUT_READY_LOCK:
            if root_key in _LAYOUT_READY:
                return

        self.root.mkdir(parents=True, exist_ok=True)
        readme = self.root / "README.md"
        should_write_readme = True
        if readme.exists():
            try:
                current = readme.read_text(encoding="utf-8")
                should_write_readme = current != _README_TEXT or any(
                    ord(char) >= 128 for char in current
                )
            except UnicodeDecodeError:
                should_write_readme = True
        if should_write_readme:
            readme.write_text(_README_TEXT, encoding="utf-8")
        with _LAYOUT_READY_LOCK:
            _LAYOUT_READY.add(root_key)

    async def ensure_layout_async(self) -> None:
        """异步 ensure_layout：盘 IO 用 anyio，已就绪则跳过。"""
        from jiuwenswarm.server.runtime.async_fs import (
            async_exists,
            async_mkdir,
            async_read_text,
            async_write_text,
        )

        root_key = str(self.root.resolve())
        with _LAYOUT_READY_LOCK:
            if root_key in _LAYOUT_READY:
                return

        await async_mkdir(self.root, parents=True, exist_ok=True)
        readme = self.root / "README.md"
        should_write_readme = True
        if await async_exists(readme):
            try:
                current = await async_read_text(readme)
                should_write_readme = current != _README_TEXT or any(
                    ord(char) >= 128 for char in current
                )
            except UnicodeDecodeError:
                should_write_readme = True
        if should_write_readme:
            await async_write_text(readme, _README_TEXT)
        with _LAYOUT_READY_LOCK:
            _LAYOUT_READY.add(root_key)

    def load_session_attachments(self, session_id: str) -> list[PromptAttachment]:
        """Load prompt attachments for one jiuwenswarm session."""

        return self.file_store.list(session_id=session_id)

    async def sync_to_agent(self, agent: Any, *, session_id: str) -> None:
        """Synchronize current prompt attachment files to a DeepAgent instance.

        Loader failures are intentionally non-fatal. User requests must continue
        even if one prompt attachment file is unreadable.
        """

        try:
            manager = getattr(agent, "prompt_attachment_manager", None)
            if manager is None:
                raise AttributeError("agent.prompt_attachment_manager is unavailable")
        except Exception as exc:
            logger.warning("[PromptAttachmentLoader] failed to get PromptAttachmentManager: %s", exc)
            return

        try:
            session_attachments = self.load_session_attachments(session_id)
        except Exception as exc:
            logger.warning("[PromptAttachmentLoader] failed to load prompt attachment directory: %s", exc)
            return
        logger.info(
            "[PromptAttachmentLoader] sync prompt attachments: session_id=%s session=%d",
            session_id,
            len(session_attachments),
        )

        try:
            await manager.replace_source(
                source=SESSION_SOURCE,
                prompt_attachments=session_attachments,
                session_id=session_id,
            )
        except Exception as exc:
            logger.warning("[PromptAttachmentLoader] failed to sync session prompt attachments: %s", exc)

    @staticmethod
    def kind_for_file(path: Path) -> PromptAttachmentKind:
        return _KIND_BY_STEM.get(path.stem, PromptAttachmentKind.TEXT)

    @staticmethod
    def relative_key(path: Path, session_dir: Path) -> str:
        return PromptAttachmentFileStore.relative_key(path, session_dir)


__all__ = [
    "DEFAULT_MAX_FILE_CHARS",
    "PromptAttachmentContextStore",
    "PromptAttachmentFileStore",
    "PromptAttachmentLoader",
    "PromptAttachmentSessionStore",
    "SESSION_SOURCE",
    "sanitize_session_id",
]
