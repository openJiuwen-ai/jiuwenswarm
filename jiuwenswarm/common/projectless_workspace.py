"""Cross-platform workspaces for Agent/Code requests without a project.

The persistent Agent workspace stores JiuwenSwarm's own state.  User-facing
files created by a projectless task live in a separate task directory under
the user's Documents folder instead:

    <Documents>/JiuwenSwarm/<YYYY-MM-DD>/chat-<n>/
        work/
        outputs/

The task-to-directory registry keeps a session in the same directory when a
conversation is resumed on a later day or after its title changes.  New task
directories use ASCII-only ``chat-<n>`` names; the original query/title is
stored in ``metadata.json`` instead of being included in the path.  Registry
metadata is kept in JiuwenSwarm's private agent workspace rather than beside
user-facing task directories.
"""

from __future__ import annotations

import datetime as _datetime
import json
import os
import re
import sys
import unicodedata
import uuid
from dataclasses import dataclass
from pathlib import Path


_TASKS_DIR_ENV = "JIUWENSWARM_TASKS_DIR"
_TASK_REGISTRY_DIR_ENV = "JIUWENSWARM_TASK_REGISTRY_DIR"
_REGISTRY_SUBDIR = ".projectless_tasks"
_LEGACY_REGISTRY_DIR = ".jiuwenswarm"
_CHAT_DIR_PREFIX = "chat"
_METADATA_FILENAME = "metadata.json"
_MAX_TASK_NAME_LENGTH = 48
_WINDOWS_RESERVED_NAMES = {
    "AUX",
    "CLOCK$",
    "COM1",
    "COM2",
    "COM3",
    "COM4",
    "COM5",
    "COM6",
    "COM7",
    "COM8",
    "COM9",
    "CON",
    "LPT1",
    "LPT2",
    "LPT3",
    "LPT4",
    "LPT5",
    "LPT6",
    "LPT7",
    "LPT8",
    "LPT9",
    "NUL",
    "PRN",
}


@dataclass(frozen=True, slots=True)
class ProjectlessTaskWorkspace:
    """The filesystem locations exposed to one projectless Agent task."""

    root_dir: Path
    work_dir: Path
    outputs_dir: Path


def get_projectless_tasks_dir() -> Path:
    """Return the task root below the platform's Documents directory.

    ``JIUWENSWARM_TASKS_DIR`` remains the authoritative deployment override.
    Otherwise Windows honors the user's configured Documents known folder,
    Linux honors ``user-dirs.dirs``, and macOS uses ``~/Documents``.
    """
    configured = os.environ.get(_TASKS_DIR_ENV, "").strip()
    base = (
        Path(configured).expanduser()
        if configured
        else _get_documents_dir() / "JiuwenSwarm"
    )
    return base.resolve()


def _get_documents_dir() -> Path:
    if sys.platform == "win32":
        configured = _get_windows_documents_dir()
        if configured is not None:
            return configured
    elif sys.platform.startswith("linux"):
        configured = _get_linux_documents_dir()
        if configured is not None:
            return configured
    return Path.home() / "Documents"


def _get_windows_documents_dir() -> Path | None:
    try:
        import winreg

        key_path = (
            r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
        )
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, key_path) as key:
            raw_value, _ = winreg.QueryValueEx(key, "Personal")
        value = os.path.expandvars(str(raw_value)).strip()
        return Path(value).expanduser() if value else None
    except (ImportError, OSError, TypeError):
        return None


def _get_linux_documents_dir() -> Path | None:
    config_home = Path(
        os.environ.get("XDG_CONFIG_HOME", "").strip() or Path.home() / ".config"
    )
    user_dirs_path = config_home / "user-dirs.dirs"
    try:
        lines = user_dirs_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    for line in lines:
        match = re.match(r'^\s*XDG_DOCUMENTS_DIR\s*=\s*"(.*)"\s*$', line)
        if match is None:
            continue
        value = match.group(1).replace("$HOME", str(Path.home()))
        value = os.path.expandvars(value).strip()
        return Path(value).expanduser() if value else None
    return None


def get_projectless_task_workspace(
    session_id: str | None = None,
    task_name: str | None = None,
) -> ProjectlessTaskWorkspace:
    """Create or reuse a stable workspace for a projectless task session."""
    tasks_dir = get_projectless_tasks_dir()
    safe_session = _slugify(session_id, fallback="default")
    registered_root = _read_registered_root(tasks_dir, safe_session)
    if registered_root is None:
        task_date = _datetime.datetime.now().astimezone().strftime("%Y-%m-%d")
        registered_root = _allocate_task_root(
            tasks_dir,
            task_date,
            safe_session,
        )
        _write_registered_root(tasks_dir, safe_session, registered_root)
        _write_task_metadata(
            registered_root,
            session_id=session_id,
            task_name=task_name,
        )
    elif not (registered_root / _METADATA_FILENAME).exists():
        # Backfill metadata for a workspace created by an earlier version.
        _write_task_metadata(
            registered_root,
            session_id=session_id,
            task_name=task_name,
        )

    work_dir = registered_root / "work"
    outputs_dir = registered_root / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    return ProjectlessTaskWorkspace(
        root_dir=registered_root,
        work_dir=work_dir,
        outputs_dir=outputs_dir,
    )


def _slugify(value: str | None, *, fallback: str) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE)
    text = text.strip(" .-_")
    text = text[:_MAX_TASK_NAME_LENGTH].rstrip(" .-_")
    if not text:
        return fallback
    if text.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f"task-{text}"
    return text


def _allocate_task_root(
    tasks_dir: Path,
    task_date: str,
    safe_session: str,
) -> Path:
    date_dir = tasks_dir / task_date
    date_dir.mkdir(parents=True, exist_ok=True)
    index = 1
    while True:
        candidate = date_dir / f"{_CHAT_DIR_PREFIX}-{index}"
        if candidate.exists():
            if _read_session_marker(candidate) == safe_session:
                return candidate.resolve()
            index += 1
            continue
        try:
            candidate.mkdir(parents=True)
        except FileExistsError:
            continue
        _write_session_marker(candidate, safe_session)
        return candidate.resolve()


def _registry_root() -> Path:
    """Return the private registry directory for projectless task bindings."""
    configured = os.environ.get(_TASK_REGISTRY_DIR_ENV, "").strip()
    if configured:
        return Path(configured).expanduser().resolve()

    # Import lazily so the workspace module remains cheap and avoids coupling
    # its import order to the broader JiuwenSwarm bootstrap sequence.
    from jiuwenswarm.common.utils import get_agent_workspace_dir

    return (get_agent_workspace_dir() / _REGISTRY_SUBDIR).resolve()


def _registry_path(safe_session: str) -> Path:
    return _registry_root() / f"{safe_session}.json"


def _legacy_registry_path(tasks_dir: Path, safe_session: str) -> Path:
    return tasks_dir / _LEGACY_REGISTRY_DIR / f"{safe_session}.json"


def _read_registered_root(tasks_dir: Path, safe_session: str) -> Path | None:
    primary_path = _registry_path(safe_session)
    for path in (primary_path, _legacy_registry_path(tasks_dir, safe_session)):
        if path == primary_path and not path.exists():
            continue
        try:
            record = json.loads(path.read_text(encoding="utf-8"))
            raw_root = record.get("root_dir") if isinstance(record, dict) else None
            if not isinstance(raw_root, str) or not raw_root.strip():
                continue
            root = Path(raw_root).expanduser().resolve()
            if not root.is_relative_to(tasks_dir.resolve()) or not root.is_dir():
                continue
            if path != primary_path:
                _write_registered_root(tasks_dir, safe_session, root)
            return root
        except (OSError, ValueError, TypeError):
            continue
    return None


def _write_registered_root(tasks_dir: Path, safe_session: str, root: Path) -> None:
    registry_dir = _registry_root()
    registry_dir.mkdir(parents=True, exist_ok=True)
    path = _registry_path(safe_session)
    # A unique sibling keeps concurrent turns for the same session from
    # clobbering one another's temporary registry file before os.replace().
    temporary = path.with_name(f".{path.name}.{uuid.uuid4().hex}.tmp")
    try:
        temporary.write_text(
            json.dumps({"root_dir": str(root)}, ensure_ascii=False),
            encoding="utf-8",
        )
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _write_task_metadata(
    root: Path,
    *,
    session_id: str | None,
    task_name: str | None,
) -> None:
    """Persist query/title separately from the ASCII-only workspace name."""
    metadata_path = root / _METADATA_FILENAME
    now = _datetime.datetime.now().astimezone().isoformat()
    query = str(task_name or "")
    metadata = {
        "chat_id": root.name,
        "session_id": str(session_id or ""),
        "title": query,
        "query": query,
        "created_at": now,
    }
    temporary = metadata_path.with_name(
        f".{metadata_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary.write_text(
            json.dumps(metadata, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, metadata_path)
    finally:
        temporary.unlink(missing_ok=True)


def _session_marker_path(root: Path) -> Path:
    return root / ".session_id"


def _read_session_marker(root: Path) -> str | None:
    try:
        marker = _session_marker_path(root).read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return marker or None


def _write_session_marker(root: Path, safe_session: str) -> None:
    try:
        _session_marker_path(root).write_text(safe_session, encoding="utf-8")
    except OSError:
        # The registry remains the source of truth.  A marker is only needed
        # to disambiguate a title collision during allocation.
        pass
