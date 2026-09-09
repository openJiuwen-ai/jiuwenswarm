"""Cross-platform workspaces for Agent/Code requests without a project.

The persistent Agent workspace stores JiuwenSwarm's own state.  User-facing
files created by a projectless task live in a separate task directory under
the user's Documents folder instead:

    <Documents>/JiuwenSwarm/<YYYY-MM-DD>/chat-<n>/
        work/
        outputs/

The date directory is based on the session's creation date, so a conversation
that first sends a projectless request on a later day still starts under the
session's original date.  The task-to-directory registry keeps a session in
the same directory when it is resumed on a later day or after its title
changes.  New task directories use ASCII-only ``chat-<n>`` names; the original
query/title is stored in ``metadata.json`` instead of being included in the
path.  Registry metadata is kept in JiuwenSwarm's private agent workspace
rather than beside user-facing task directories.
"""

from __future__ import annotations

import datetime as _datetime
import json
import math
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
    """Create or reuse a stable workspace for a projectless task session.

    The date directory is anchored to the session's creation time, rather than
    the time of the first request that needs a projectless workspace.  This is
    important when ``session.create`` and the first ``chat.send`` happen on
    different calendar days.  Legacy sessions without usable metadata fall
    back to the current local date.
    """
    tasks_dir = get_projectless_tasks_dir()
    root = allocate_task_workspace(
        tasks_dir,
        registry_dir=None,
        session_id=session_id,
        task_name=task_name,
    )
    work_dir = root / "work"
    outputs_dir = root / "outputs"
    work_dir.mkdir(parents=True, exist_ok=True)
    outputs_dir.mkdir(parents=True, exist_ok=True)
    return ProjectlessTaskWorkspace(
        root_dir=root,
        work_dir=work_dir,
        outputs_dir=outputs_dir,
    )


def allocate_task_workspace(
    tasks_dir: Path,
    *,
    registry_dir: Path | None,
    session_id: str | None,
    task_name: str | None,
) -> Path:
    """Allocate or reuse a ``chat-<n>`` task root under ``tasks_dir``.

    Shared by single-agent projectless runs (``tasks_dir`` = the Documents
    task root, ``registry_dir`` left None so it falls back to the private
    agent-workspace registry) and by team artifact allocation (``tasks_dir``
    = ``<team-workspace>/artifacts``, ``registry_dir`` = the artifacts'
    own ``.team_artifacts`` subdir so a relocated team workspace stays
    self-contained). The layout — dated ``chat-<n>`` directories, the
    ``.session_id`` collision marker and the ``metadata.json`` sidecar —
    is identical in both cases, so a future unification can fold the
    callers together without touching the on-disk shape.

    Args:
        tasks_dir: Root under which dated ``<YYYY-MM-DD>/chat-<n>`` task
            directories are allocated.
        registry_dir: Directory holding the ``<safe_session>.json`` ->
            root binding registry. When ``None``, the single-agent
            private registry (``_registry_root()``) is used so existing
            behaviour is preserved exactly.
        session_id: Session id driving reuse (same session -> same root).
        task_name: Optional human-readable title persisted to
            ``metadata.json``; never used in the path.

    Returns:
        The resolved task root directory (created or reused).
    """
    tasks_dir = tasks_dir.resolve()
    safe_session = slugify(session_id, fallback="default")
    registered_root = _read_registered_root(tasks_dir, safe_session, registry_dir)
    if registered_root is None:
        task_date = _get_session_task_date(session_id) or _local_today()
        registered_root = _allocate_task_root(
            tasks_dir,
            task_date,
            safe_session,
        )
        _write_registered_root(safe_session, registered_root, registry_dir)
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
    return registered_root


def _local_today() -> str:
    """Return today's date in the host's local timezone."""
    return _datetime.datetime.now().astimezone().strftime("%Y-%m-%d")


def _get_session_task_date(session_id: str | None) -> str | None:
    """Read a session's immutable creation date for task-directory binding.

    ``session_metadata`` stores ``created_at`` as a UTC Unix timestamp.  The
    task directory is user-facing, so the timestamp is converted to the local
    timezone before deriving ``YYYY-MM-DD``.  The filesystem creation time is
    used only as a compatibility fallback for old sessions that have no
    readable ``metadata.json``.
    """
    raw_session = str(session_id or "").strip()
    session_path = Path(raw_session)
    if not raw_session:
        return None
    if raw_session in {".", ".."}:
        return None
    if any(separator in raw_session for separator in ("/", "\\")):
        return None
    if session_path.is_absolute():
        return None
    if session_path.name != raw_session:
        return None

    try:
        # Keep this dependency lazy: projectless workspace resolution is used
        # during bootstrap, while the session metadata module imports the
        # broader runtime stack.
        from jiuwenswarm.common.utils import get_agent_sessions_dir

        session_dir = get_agent_sessions_dir() / raw_session
    except (OSError, TypeError, ValueError):
        return None

    timestamp = None
    try:
        metadata_path = session_dir / _METADATA_FILENAME
        metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        created_at = (
            metadata.get("created_at") if isinstance(metadata, dict) else None
        )
        timestamp = _coerce_timestamp(created_at)
    except (OSError, TypeError, ValueError, OverflowError):
        pass

    if timestamp is None:
        try:
            timestamp = float(session_dir.stat().st_ctime)
        except (OSError, ValueError, OverflowError):
            return None

    try:
        local_timezone = _datetime.datetime.now().astimezone().tzinfo
        return _datetime.datetime.fromtimestamp(
            timestamp,
            tz=local_timezone,
        ).strftime("%Y-%m-%d")
    except (OSError, OverflowError, ValueError):
        return None


def _coerce_timestamp(value: object) -> float | None:
    """Convert numeric or ISO-8601 metadata timestamps to Unix seconds."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
    elif isinstance(value, str) and value.strip():
        raw_value = value.strip()
        try:
            timestamp = float(raw_value)
        except ValueError:
            try:
                parsed = _datetime.datetime.fromisoformat(
                    raw_value.replace("Z", "+00:00")
                )
            except ValueError:
                return None
            if parsed.tzinfo is None:
                parsed = parsed.replace(
                    tzinfo=_datetime.datetime.now().astimezone().tzinfo
                )
            timestamp = parsed.timestamp()
    else:
        return None

    return timestamp if timestamp > 0 and math.isfinite(timestamp) else None


def slugify(
    value: str | None, *, fallback: str, reserved_prefix: str = "task"
) -> str:
    """Reduce a free-form value to a stable, filesystem-safe name fragment.

    Args:
        value: The raw value (session id, member name, task title).
        fallback: Returned when the value reduces to empty.
        reserved_prefix: Prefix prepended when the result would collide
            with a Windows reserved device name (``CON``, ``NUL`` ...).
            Only a defensive collision-avoidance guard; the prefix is
            never part of the normal path.
    """
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[^\w.-]+", "-", text, flags=re.UNICODE)
    text = text.strip(" .-_")
    text = text[:_MAX_TASK_NAME_LENGTH].rstrip(" .-_")
    if not text:
        return fallback
    if text.split(".", 1)[0].upper() in _WINDOWS_RESERVED_NAMES:
        return f"{reserved_prefix}-{text}"
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


def _resolve_registry_dir(registry_dir: Path | None) -> Path:
    """Return the registry directory, falling back to the private default."""
    if registry_dir is not None:
        return registry_dir.resolve()
    return _registry_root()


def _registry_path(safe_session: str, registry_dir: Path | None = None) -> Path:
    return _resolve_registry_dir(registry_dir) / f"{safe_session}.json"


def _legacy_registry_path(tasks_dir: Path, safe_session: str) -> Path:
    return tasks_dir / _LEGACY_REGISTRY_DIR / f"{safe_session}.json"


def _read_registered_root(
    tasks_dir: Path,
    safe_session: str,
    registry_dir: Path | None = None,
) -> Path | None:
    primary_path = _registry_path(safe_session, registry_dir)
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
                _write_registered_root(safe_session, root, registry_dir)
            return root
        except (OSError, ValueError, TypeError):
            continue
    return None


def _write_registered_root(
    safe_session: str,
    root: Path,
    registry_dir: Path | None = None,
) -> None:
    resolved_dir = _resolve_registry_dir(registry_dir)
    resolved_dir.mkdir(parents=True, exist_ok=True)
    path = _registry_path(safe_session, registry_dir)
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
