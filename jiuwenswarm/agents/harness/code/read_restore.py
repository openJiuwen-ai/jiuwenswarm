# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Per-session record of the files the agent has seen, for ``ReadRestoreRail``.

agent-core refuses ``edit_file`` / ``write_file`` on a file that ``read_file``
has not read *in the current process*: its read registry is a module-level
dict. A Code session that continues in a new process (the headless implement
driver starts one per continuation round; the desktop after an AgentServer
restart) therefore loses every read and the model must read each file again.

This module keeps a small durable record per session of the files the agent
read, edited or wrote, with the file's size, mtime and content hash at that
moment. ``ReadRestoreRail`` uses it to re-register a read through agent-core's
own ``read_file`` tool when, and only when, the file still has the content the
agent saw. Everything is stored under the agent root, never inside a project.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import re
import tempfile
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

STORE_DIRNAME = "read_restore"
STORE_VERSION = 1
# Files above this size are not hashed and not recorded: restoring them would
# rest on mtime+size alone, and agent-core's own size caps make such reads rare.
MAX_HASH_BYTES = 8 * 1024 * 1024
MAX_FILES_PER_SESSION = 400
MAX_SESSION_FILES = 500

KIND_READ = "read"
KIND_EDIT = "edit"
KIND_WRITE = "write"

_NON_TEXT_SUFFIXES = (
    ".pdf",
    ".ipynb",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".bmp",
    ".webp",
    ".tif",
    ".tiff",
)


def is_text_path(path: str) -> bool:
    """Mirror agent-core: only text reads are recorded for edits."""
    return not str(path).lower().endswith(_NON_TEXT_SUFFIXES)


def safe_session_name(session_id: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(session_id or "")).strip("._-")
    if not safe:
        return ""
    if len(safe) > 80:
        digest = hashlib.sha256(safe.encode("utf-8")).hexdigest()[:16]
        safe = f"{safe[:60]}_{digest}"
    return safe


def default_store_root() -> Path:
    from jiuwenswarm.common.utils import get_agent_root_dir

    return Path(get_agent_root_dir()) / STORE_DIRNAME


# ─── file fingerprint ─────────────────────────────────────────


@dataclass(frozen=True)
class Fingerprint:
    mtime_ns: int
    size: int
    sha256: str
    lines: int


def fingerprint(path: str | os.PathLike[str]) -> Fingerprint | None:
    """Stat, hash and count the lines of ``path``; ``None`` when unusable."""
    try:
        st = os.stat(path)
    except OSError:
        return None
    if not os.path.isfile(path) or st.st_size > MAX_HASH_BYTES:
        return None
    try:
        with open(path, "rb") as fh:
            raw = fh.read(MAX_HASH_BYTES + 1)
    except OSError:
        return None
    if len(raw) > MAX_HASH_BYTES:
        return None
    text = raw.decode("utf-8", errors="replace").replace("\r\n", "\n")
    lines = len(text.splitlines()) if text else 0
    return Fingerprint(
        mtime_ns=st.st_mtime_ns,
        size=st.st_size,
        sha256=hashlib.sha256(raw).hexdigest(),
        lines=lines,
    )


def merge_range(ranges: list[list[int]], start: int, end: int) -> list[list[int]]:
    items = sorted([list(r) for r in ranges] + [[start, end]])
    merged: list[list[int]] = []
    for s, e in items:
        if merged and s <= merged[-1][1] + 1:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    return merged


def ranges_cover(ranges: list[list[int]], total: int) -> bool:
    covered = 0
    for s, e in sorted(ranges):
        if s > covered + 1:
            return False
        covered = max(covered, e)
    return total > 0 and covered >= total


# ─── store ────────────────────────────────────────────────────


@dataclass
class FileRecord:
    kind: str
    mtime_ns: int
    size: int
    sha256: str
    lines: int
    ranges: list[list[int]] = field(default_factory=list)
    updated: float = 0.0

    @property
    def complete(self) -> bool:
        """Whole file seen through ``read_file`` reads (agent-core's write rule)."""
        return self.kind == KIND_READ and ranges_cover(self.ranges, self.lines)

    def to_json(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "mtime_ns": self.mtime_ns,
            "size": self.size,
            "sha256": self.sha256,
            "lines": self.lines,
            "ranges": self.ranges,
            "updated": self.updated,
        }

    @classmethod
    def from_json(cls, data: Mapping[str, Any]) -> FileRecord | None:
        try:
            ranges = [
                [int(s), int(e)]
                for s, e in (data.get("ranges") or [])
                if int(s) >= 1 and int(e) >= int(s)
            ]
            return cls(
                kind=str(data["kind"]),
                mtime_ns=int(data["mtime_ns"]),
                size=int(data["size"]),
                sha256=str(data["sha256"]),
                lines=int(data["lines"]),
                ranges=ranges,
                updated=float(data.get("updated") or 0.0),
            )
        except (KeyError, TypeError, ValueError):
            return None

    def matches(self, current: Fingerprint) -> bool:
        """The file still has the content the agent saw."""
        return self.size == current.size and self.sha256 == current.sha256


class SessionReadStore:
    """Durable per-session map ``path -> FileRecord`` (JSON, atomic writes)."""

    def __init__(self, root: Path | Callable[[], Path] | None = None) -> None:
        self._root = root

    def root(self) -> Path:
        if callable(self._root):
            return Path(self._root())
        if self._root is not None:
            return Path(self._root)
        return default_store_root()

    def _path(self, session_id: str) -> Path | None:
        name = safe_session_name(session_id)
        if not name:
            return None
        return self.root() / f"{name}.json"

    def load(self, session_id: str) -> dict[str, FileRecord]:
        path = self._path(session_id)
        if path is None or not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            logger.warning("[ReadRestore] unreadable store ignored: %s", path)
            return {}
        if not isinstance(data, dict) or data.get("version") != STORE_VERSION:
            return {}
        if data.get("session") != str(session_id):
            return {}
        out: dict[str, FileRecord] = {}
        for key, value in (data.get("files") or {}).items():
            if isinstance(value, Mapping):
                rec = FileRecord.from_json(value)
                if rec is not None:
                    out[str(key)] = rec
        return out

    def get(self, session_id: str, path: str) -> FileRecord | None:
        files = self.load(session_id)
        rec = files.get(path)
        if rec is not None:
            return rec
        # Same file under another spelling (relative/absolute, symlinked tmp).
        try:
            real = os.path.realpath(path)
        except OSError:
            return None
        for key, value in files.items():
            try:
                if os.path.realpath(key) == real:
                    return value
            except OSError:
                continue
        return None

    def put(self, session_id: str, path: str, record: FileRecord) -> None:
        target = self._path(session_id)
        if target is None:
            return
        files = self.load(session_id)
        record.updated = time.time()
        files[path] = record
        if len(files) > MAX_FILES_PER_SESSION:
            keep = sorted(files.items(), key=lambda kv: kv[1].updated, reverse=True)
            files = dict(keep[:MAX_FILES_PER_SESSION])
        payload = {
            "version": STORE_VERSION,
            "session": str(session_id),
            "files": {k: v.to_json() for k, v in files.items()},
        }
        new_file = not target.exists()
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(
                dir=str(target.parent), prefix=".rr-", suffix=".tmp"
            )
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                json.dump(payload, fh)
            os.replace(tmp, target)
        except OSError as exc:
            logger.warning("[ReadRestore] store write failed for %s: %s", target, exc)
            return
        if new_file:
            self._prune()

    def _prune(self) -> None:
        try:
            files = sorted(
                (p for p in self.root().glob("*.json") if p.is_file()),
                key=lambda p: p.stat().st_mtime,
                reverse=True,
            )
        except OSError:
            return
        for stale in files[MAX_SESSION_FILES:]:
            try:
                stale.unlink()
            except OSError:
                pass


def record_read(
    store: SessionReadStore,
    session_id: str,
    path: str,
    *,
    offset: int,
    line_count: int,
) -> FileRecord | None:
    """Record a successful ``read_file`` of ``path`` (a window or the whole file)."""
    if not is_text_path(path):
        return None
    current = fingerprint(path)
    if current is None:
        return None
    previous = store.get(session_id, path)
    ranges: list[list[int]] = []
    if (
        previous is not None
        and previous.kind == KIND_READ
        and previous.matches(current)
    ):
        ranges = list(previous.ranges)
    if line_count > 0:
        start = max(1, int(offset) + 1)
        ranges = merge_range(ranges, start, start + int(line_count) - 1)
    record = FileRecord(
        kind=KIND_READ,
        mtime_ns=current.mtime_ns,
        size=current.size,
        sha256=current.sha256,
        lines=current.lines,
        ranges=ranges,
    )
    store.put(session_id, path, record)
    return record


def record_change(
    store: SessionReadStore, session_id: str, path: str, kind: str
) -> FileRecord | None:
    """Record a successful ``edit_file`` / ``write_file``: the agent knows the new content."""
    if not is_text_path(path):
        return None
    current = fingerprint(path)
    if current is None:
        return None
    record = FileRecord(
        kind=kind,
        mtime_ns=current.mtime_ns,
        size=current.size,
        sha256=current.sha256,
        lines=current.lines,
    )
    store.put(session_id, path, record)
    return record


# ─── read policy ──────────────────────────────────────────────


def _level_name(level: Any) -> str:
    value = getattr(level, "value", level)
    return str(value or "").strip().lower()


def read_denied_reason(
    path: str,
    *,
    project_dir: str | None,
    permissions: Mapping[str, Any] | None,
    session_id: str | None,
) -> str | None:
    """Why a restore of ``path`` must not happen under the current policy.

    A restore only re-registers a read the agent already made, but the policy
    may have changed since. The project policy (deny-only
    ``.jiuwenswarm/permissions.yaml``, optional package) and, when the
    permission engine is enabled, its ``read_file`` decision for the path are
    checked; ``deny`` or ``ask`` blocks the restore, so the model's own
    ``read_file`` goes through the normal approval. Any error blocks it too.
    """
    permissions = permissions if isinstance(permissions, Mapping) else {}
    candidates = [Path(path)]
    try:
        real = Path(os.path.realpath(path))
        if real != candidates[0]:
            candidates.append(real)
    except OSError:
        return "path could not be resolved"
    try:
        reason = _project_denied(candidates, project_dir, permissions)
        if reason:
            return reason
        if permissions.get("enabled"):
            reason = _engine_denied(candidates, project_dir, permissions, session_id)
            if reason:
                return reason
    except Exception as exc:  # noqa: BLE001 - fail closed
        logger.warning("[ReadRestore] read policy check failed for %s: %s", path, exc)
        return "read policy check failed"
    return None


def _project_denied(
    candidates: list[Path], project_dir: str | None, permissions: Mapping[str, Any]
) -> str | None:
    if not project_dir:
        return None
    try:
        from jiuwenswarm.agents.harness.common.rails.permissions.project_policy import (
            load_project_policy,
        )
    except ImportError:
        return None
    policy = load_project_policy(str(project_dir), config=dict(permissions or {}))
    if getattr(policy, "error", None):
        return "project policy file cannot be used"
    if not getattr(policy, "deny_paths", None):
        return None
    for candidate in candidates:
        if policy.path_denied(candidate, "read") is not None:
            return "project policy denies reading it"
    return None


def _engine_denied(
    candidates: list[Path],
    project_dir: str | None,
    permissions: Mapping[str, Any],
    session_id: str | None,
) -> str | None:
    from openjiuwen.harness.security.permission_engine.core import PermissionEngine
    from openjiuwen.harness.security.permission_engine.fileguard.file_guard import (
        build_file_guard_checker,
    )

    from jiuwenswarm.agents.harness.common.rails.permissions.permission_compose import (
        compose_host_effective_permissions,
    )
    from jiuwenswarm.agents.harness.common.rails.permissions.permissions_layers import (
        load_session_permissions,
        load_user_permissions,
    )

    effective = compose_host_effective_permissions(
        global_permissions=dict(permissions),
        user_permissions=load_user_permissions(),
        session_permissions=load_session_permissions(session_id or None),
        session_id=session_id or None,
    )
    if not effective.get("enabled", True):
        return None
    root = Path(project_dir) if project_dir else candidates[0].parent
    engine = PermissionEngine(
        config=dict(effective), workspace_root=root, trusted_dirs=[]
    )
    guard = build_file_guard_checker(
        engine.config, workspace_root=root, trusted_dirs=[]
    )
    for candidate in candidates:
        args = {"file_path": str(candidate)}
        level, _rule = engine.evaluate_global_policy_directly(
            "read_file", args, include_external_directory=False
        )
        if _level_name(level) in ("deny", "ask"):
            return f"permission rules: {_level_name(level)}"
        if guard is not None:
            result = guard.evaluate("read_file", args)
            if result is not None and _level_name(
                getattr(result, "permission", None)
            ) in (
                "deny",
                "ask",
            ):
                return f"file guard: {_level_name(getattr(result, 'permission', None))}"
    return None


__all__ = [
    "KIND_EDIT",
    "KIND_READ",
    "KIND_WRITE",
    "FileRecord",
    "Fingerprint",
    "SessionReadStore",
    "default_store_root",
    "fingerprint",
    "is_text_path",
    "read_denied_reason",
    "record_change",
    "record_read",
]
