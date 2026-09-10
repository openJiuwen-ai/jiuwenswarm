# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""File / share / upload HTTP helpers (formerly ``app_web`` /file-api + /share-api).

Enterprise list/preview of remote Agent workspace is **not** guaranteed (1-A):
handlers read Gateway-local disks. Share uses disk history personally and
``ChatHistoryStore`` under enterprise edition (2-A). Push lands on Gateway disk
and signs a download token (3-A); no POST to Web Pod.
"""

from __future__ import annotations
from jiuwenswarm.edition import is_enterprise

import json
import logging
import mimetypes
import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import quote

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class FileHttpRoots:
    """Allowed roots for path sandbox (same semantics as legacy app_web)."""

    project_root: Path
    workspace_root: Path
    agent_teams_root: Path
    logs_root: Path
    auto_harness_root: Path


def default_file_http_roots() -> FileHttpRoots:
    from openjiuwen.agent_teams.paths import get_agent_teams_home

    from jiuwenswarm.common.utils import (
        get_logs_dir,
        get_user_workspace_dir,
    )

    project_root = get_user_workspace_dir().resolve()
    # Legacy app_web sandbox: paths like ``agent/workspace`` live under
    # ``<user_workspace>/agent/``, not only multi-tenant ``get_agent_root_dir()``.
    workspace_root = (project_root / "agent").resolve()
    return FileHttpRoots(
        project_root=project_root,
        workspace_root=workspace_root,
        agent_teams_root=get_agent_teams_home().resolve(),
        logs_root=get_logs_dir().resolve(),
        auto_harness_root=(project_root / "auto-harness").resolve(),
    )


def is_markdown(path_obj: Path) -> bool:
    return path_obj.suffix.lower() in {".md", ".mdx"}


def is_path_under_allowed_root(roots: FileHttpRoots, target: Path) -> bool:
    return _is_under_file_roots(roots, target)


def is_path_under_directory(base: Path, target: Path) -> bool:
    """Return True when ``target`` resolves inside ``base``."""
    try:
        base_resolved = base.resolve()
        target_resolved = target.resolve()
        return os.path.commonpath([str(base_resolved), str(target_resolved)]) == str(
            base_resolved,
        )
    except ValueError:
        return False


def _is_under_file_roots(roots: FileHttpRoots, target: Path) -> bool:
    target_resolved = target.resolve()
    try:
        checks: list[Path] = [
            roots.workspace_root,
            (roots.project_root / "agent").resolve(),
            roots.agent_teams_root,
            roots.logs_root,
            roots.auto_harness_root,
        ]
        try:
            from jiuwenswarm.common.utils import get_agent_root_dir

            bound_agent = get_agent_root_dir().resolve()
            if bound_agent not in checks:
                checks.append(bound_agent)
        except Exception:  # noqa: BLE001
            pass
        for root in checks:
            if os.path.commonpath([str(root), str(target_resolved)]) == str(root):
                return True
        return False
    except ValueError:
        return False


def safe_filename(filename: str, *, default: str = "unnamed") -> str:
    """Return a single path segment safe for joining under a sandbox root."""
    name = Path(str(filename or "")).name.strip()
    if not name or name in {".", ".."}:
        return default
    return name


def resolve_path_under_directory(base: Path, *parts: str) -> Path | None:
    """Join ``parts`` under ``base``; return None when the result escapes ``base``."""
    base_resolved = base.resolve()
    candidate = base_resolved.joinpath(*parts).resolve()
    if is_path_under_directory(base_resolved, candidate):
        return candidate
    return None


def is_download_path_allowed(target: Path, roots: FileHttpRoots | None = None) -> bool:
    """Token downloads must stay under push inbox or configured file roots."""
    resolved = target.resolve()
    if is_path_under_directory(received_files_dir(), resolved):
        return True
    if roots is not None and is_path_under_allowed_root(roots, resolved):
        return True
    return False


def resolve_under_project(roots: FileHttpRoots, rel_or_abs: str) -> Path:
    return (roots.project_root / rel_or_abs).resolve()


def _normalize_lang_suffix(name: str) -> str:
    stem, suffix = name.rpartition(".")[0], name.rpartition(".")[2]
    suffix_lower = suffix.lower()
    if suffix_lower in ("md", "mdx"):
        stem_lower = stem.lower()
        if stem_lower.endswith("_zh"):
            stem = stem[:-3]
        elif stem_lower.endswith("_en"):
            stem = stem[:-3]
    return f"{stem}.{suffix}" if stem else name


def generate_agent_data(project_root: Path) -> None:
    """Generate agent/jiuwenclaw_workspace/agent-data.json from agent tree."""
    agent_root = (project_root / "agent").resolve()
    workspace_root = (agent_root / "jiuwenclaw_workspace").resolve()
    output_path = (workspace_root / "agent-data.json").resolve()
    root_folder_key = "__root__"

    if not agent_root.exists():
        raise FileNotFoundError("agent directory not found")
    if not agent_root.is_dir():
        raise NotADirectoryError("agent is not a directory")

    folder_data: dict[str, list[dict[str, str | bool]]] = {}
    seen_paths: dict[str, set[str]] = {}
    for entry in sorted(workspace_root.rglob("*")):
        if not entry.is_file() or entry.name.startswith("."):
            continue
        if any(part.startswith(".") for part in entry.relative_to(workspace_root).parts):
            continue
        relative_folder_path = entry.parent.relative_to(agent_root).as_posix()
        folder_key = root_folder_key if relative_folder_path == "." else relative_folder_path

        display_name = _normalize_lang_suffix(entry.name)
        display_path = (
            f"agent/{relative_folder_path}/{display_name}".replace("/.", "/").replace("//", "/")
            if relative_folder_path != "."
            else f"agent/{display_name}"
        )
        seen = seen_paths.setdefault(folder_key, set())
        if display_path in seen:
            continue
        seen.add(display_path)

        folder_data.setdefault(folder_key, []).append(
            {
                "name": display_name,
                "path": display_path,
                "isMarkdown": entry.suffix.lower() in {".md", ".mdx"},
            }
        )

    sorted_folder_data = {
        folder_key: sorted(files, key=lambda item: item["path"])
        for folder_key, files in sorted(folder_data.items(), key=lambda item: item[0])
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(sorted_folder_data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def parse_single_byte_range(
    range_header: str,
    file_size: int,
) -> tuple[int, int] | None:
    """Parse one HTTP byte range, returning inclusive start and end offsets."""
    if file_size == 0 or not range_header.startswith("bytes=") or "," in range_header:
        return None

    range_value = range_header[6:]
    if "-" not in range_value:
        return None

    start_text, end_text = range_value.split("-", 1)
    if not start_text:
        if not end_text.isascii() or not end_text.isdecimal():
            return None
        suffix_length = int(end_text)
        if suffix_length <= 0:
            return None
        return max(0, file_size - suffix_length), file_size - 1

    if not start_text.isascii() or not start_text.isdecimal():
        return None
    if end_text and (not end_text.isascii() or not end_text.isdecimal()):
        return None

    start = int(start_text)
    end = int(end_text) if end_text else file_size - 1
    if start >= file_size or end < start:
        return None
    return start, min(end, file_size - 1)


def list_markdown(roots: FileHttpRoots, dir_arg: str) -> tuple[int, dict[str, Any]]:
    if not dir_arg:
        return 400, {"error": "missing_dir"}
    full_dir = resolve_under_project(roots, dir_arg)
    if not is_path_under_allowed_root(roots, full_dir):
        return 403, {"error": "forbidden_dir"}
    if not full_dir.exists() or not full_dir.is_dir():
        return 200, {"files": []}
    files = []
    for entry in sorted(full_dir.iterdir(), key=lambda p: p.name.lower()):
        if not entry.is_file() or not is_markdown(entry):
            continue
        files.append(
            {
                "name": entry.name,
                "path": str(entry.relative_to(roots.project_root)),
            }
        )
    return 200, {"files": files}


def list_files(roots: FileHttpRoots, dir_arg: str) -> tuple[int, dict[str, Any]]:
    if not dir_arg:
        return 400, {"error": "missing_dir"}
    full_dir = resolve_under_project(roots, dir_arg)
    if not is_path_under_allowed_root(roots, full_dir):
        return 403, {"error": "forbidden_dir"}
    if not full_dir.exists() or not full_dir.is_dir():
        return 200, {"files": []}
    files = []
    entries = sorted(
        full_dir.iterdir(),
        key=lambda p: (not p.is_dir(), p.name.lower()),
    )
    for entry in entries:
        files.append(
            {
                "name": entry.name,
                "path": str(entry.relative_to(roots.project_root)),
                "isMarkdown": is_markdown(entry) if entry.is_file() else False,
                "isDirectory": entry.is_dir(),
            }
        )
    return 200, {"files": files}


def read_file_text(
    roots: FileHttpRoots,
    file_arg: str,
    encoding_arg: str = "utf-8",
) -> tuple[int, dict[str, Any] | None, bytes | None, str | None]:
    """Return (status, json_error, body_utf8, used_encoding)."""
    if not file_arg:
        return 400, {"error": "missing_file_path"}, None, None
    full_path = resolve_under_project(roots, file_arg)
    if not is_path_under_allowed_root(roots, full_path):
        return 403, {"error": "forbidden_path"}, None, None
    if not full_path.exists():
        normalized_arg = file_arg.replace("\\", "/")
        if normalized_arg in (
            "agent/jiuwenclaw_workspace/agent-data.json",
            # legacy pre-rename path (old frontend caches / bookmarks)
            "agent/workspace/agent-data.json",
        ):
            try:
                generate_agent_data(roots.project_root)
            except Exception as exc:  # noqa: BLE001
                return 500, {"error": "generate_failed", "detail": str(exc)}, None, None
            # generate_agent_data only writes to the new path; rewrite legacy
            # requests to it so the caller actually receives the data.
            if normalized_arg == "agent/workspace/agent-data.json":
                full_path = (
                    roots.project_root / "agent" / "jiuwenclaw_workspace" / "agent-data.json"
                ).resolve()
        if not full_path.exists():
            return (
                404,
                {"error": "file_not_found", "fullPath": str(full_path)},
                None,
                None,
            )

    try:
        data, used_encoding = _read_file_with_encoding(full_path, encoding_arg)
    except OSError as exc:
        return 500, {"error": str(exc)}, None, None
    return 200, None, data.encode("utf-8"), used_encoding


def _read_file_with_encoding(file_path: Path, encoding: str) -> tuple[str, str]:
    if encoding == "auto":
        import charset_normalizer

        raw_data = file_path.read_bytes()
        detected = charset_normalizer.from_bytes(raw_data).best()
        if detected is None:
            detected_encoding = "utf-8"
        else:
            detected_encoding = detected.encoding or "utf-8"
        try:
            return raw_data.decode(detected_encoding), detected_encoding
        except (UnicodeDecodeError, LookupError) as decode_exc:
            for try_encoding in ["gbk", "gb2312", "big5", "shift_jis", "euc_kr"]:
                try:
                    return raw_data.decode(try_encoding), try_encoding
                except (UnicodeDecodeError, LookupError):
                    continue
            raise OSError("Unable to decode file with any known encoding") from decode_exc
    return file_path.read_text(encoding=encoding), encoding


def write_markdown_content(
    roots: FileHttpRoots,
    request_path: Any,
    request_content: Any,
) -> tuple[int, dict[str, Any]]:
    if not isinstance(request_path, str) or not request_path.strip():
        return 400, {"error": "missing_file_path"}
    if not isinstance(request_content, str):
        return 400, {"error": "missing_file_content"}
    full_path = resolve_under_project(roots, request_path)
    if not is_path_under_allowed_root(roots, full_path):
        return 403, {"error": "forbidden_path"}
    if not is_markdown(full_path):
        return 400, {"error": "only_markdown_supported"}
    if not full_path.exists():
        return 404, {"error": "file_not_found"}
    try:
        full_path.write_text(request_content, encoding="utf-8")
    except OSError as exc:
        return 500, {"error": str(exc)}
    return 200, {"ok": True}


def resolve_raw_file_path(
    roots: FileHttpRoots,
    file_arg: str,
) -> tuple[int, dict[str, Any] | None, Path | None]:
    if not file_arg:
        return 400, {"error": "missing_file_path"}, None
    full_path = resolve_under_project(roots, file_arg)
    if not is_path_under_allowed_root(roots, full_path):
        return 403, {"error": "forbidden_path"}, None
    if not full_path.is_file():
        return 404, {"error": "file_not_found"}, None
    return 200, None, full_path


def guess_mime(path: Path | str) -> str:
    name = path.name if isinstance(path, Path) else os.path.basename(path)
    mime_type, _ = mimetypes.guess_type(name)
    return mime_type or "application/octet-stream"


def content_disposition(file_name: str, *, inline: bool) -> str:
    disposition = "inline" if inline else "attachment"
    encoded_name = quote(file_name, safe="")
    return f"{disposition}; filename*=UTF-8''{encoded_name}"


def received_files_dir() -> Path:
    """Gateway-local directory for enterprise push payloads (replaces Web Pod)."""
    override = os.getenv("JIUWENSWARM_WEB_RECEIVED_FILES", "").strip()
    if override:
        path = Path(override).expanduser().resolve()
    else:
        from jiuwenswarm.common.utils import get_user_workspace_dir

        path = (get_user_workspace_dir() / "web_received_files").resolve()
    path.mkdir(parents=True, exist_ok=True)
    return path


def save_pushed_file(
    *,
    file_bytes: bytes,
    filename: str,
    session_id: str,
) -> dict[str, Any]:
    """Persist upload and return download metadata (3-A: no Web Pod push)."""
    from jiuwenswarm.agents.harness.common.tools.web_file_download import (
        build_file_download_info,
    )

    clean_name = safe_filename(filename)
    safe_name = f"{int(time.time())}_{clean_name}"
    local_path = resolve_path_under_directory(received_files_dir(), safe_name)
    if local_path is None:
        raise ValueError("invalid_filename")
    local_path.write_bytes(file_bytes)
    download_info = build_file_download_info(
        file_path=str(local_path),
        file_name=clean_name,
        session_id=session_id,
    )
    return {
        "success": True,
        "file_path": str(local_path),
        "download_url": download_info["download_url"],
        "download_token": download_info["download_token"],
        "expires_at": download_info.get("expires_at"),
    }


def process_obs_upload_body(raw: bytes) -> tuple[int, dict[str, Any]]:
    try:
        payload = json.loads(raw.decode("utf-8") if raw else "{}")
    except json.JSONDecodeError:
        return 400, {"ok": False, "error": "invalid_json"}
    if not isinstance(payload, dict):
        return 400, {"ok": False, "error": "invalid_payload"}
    try:
        from jiuwenswarm.channels.web.minio_upload import upload_base64_payload

        return 200, upload_base64_payload(payload)
    except Exception as exc:
        logger.error("[file_http] MinIO upload failed: %s", exc, exc_info=True)
        return 500, {"ok": False, "error": str(exc)}


def _resolve_session_title(session_dir: Path, history: list[dict[str, Any]]) -> str:
    metadata_path = session_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            title = metadata.get("title") if isinstance(metadata, dict) else None
            if isinstance(title, str) and title.strip():
                return title.strip()
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass
    for record in history:
        if record.get("role") == "user":
            content = record.get("content")
            if isinstance(content, str) and content.strip():
                return content.strip().replace("\n", " ")[:80]
    return session_dir.name


def build_share_snapshot(
    *,
    session_id: str,
    user: str | None = None,
) -> tuple[dict[str, Any], str]:
    """Build share payload.

    Personal: disk session history. Enterprise: ChatHistoryStore (2-A).
    """
    exported_at = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    timestamp = time.strftime("%Y%m%d-%H%M%S")
    filename = f"jiuwenswarm-share-{timestamp}.png"

    if is_enterprise():
        from jiuwenswarm.channels.web.history_store import get_session_detail_sync

        detail = get_session_detail_sync(session_id, user=user)
        if detail is None:
            raise FileNotFoundError("history_not_found")
        messages = detail.get("messages") if isinstance(detail, dict) else None
        if not isinstance(messages, list):
            raise ValueError("invalid_history_shape")
        history_raw = [m for m in messages if isinstance(m, dict)]
        title = ""
        if isinstance(detail, dict):
            raw_title = detail.get("title")
            if isinstance(raw_title, str) and raw_title.strip():
                title = raw_title.strip()
        if not title:
            title = _resolve_session_title(Path(session_id), history_raw)
        snapshot = {
            "session_id": session_id,
            "metadata": {
                "title": title,
                "exported_at": exported_at,
                "filename": filename,
            },
            "records": history_raw,
        }
        return snapshot, filename

    from jiuwenswarm.common.utils import get_agent_sessions_dir
    from jiuwenswarm.server.runtime.session.session_history import (
        history_exists,
        load_history_records,
    )

    sessions_root = get_agent_sessions_dir().resolve()
    session_dir = (sessions_root / session_id).resolve()
    try:
        if os.path.commonpath([str(sessions_root), str(session_dir)]) != str(sessions_root):
            raise FileNotFoundError("history_not_found")
    except ValueError as exc:
        raise FileNotFoundError("history_not_found") from exc

    if not session_dir.exists() or not history_exists(session_id):
        raise FileNotFoundError("history_not_found")

    metadata_path = session_dir / "metadata.json"
    if metadata_path.exists():
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            owner = metadata.get("user_id") if isinstance(metadata, dict) else None
            if isinstance(owner, str) and owner.strip():
                if not user or owner.strip() != user.strip():
                    raise FileNotFoundError("history_not_found")
        except (OSError, json.JSONDecodeError, UnicodeDecodeError):
            pass

    try:
        history_raw = load_history_records(session_id)
    except Exception as exc:
        raise ValueError("invalid_history_json") from exc
    if not isinstance(history_raw, list):
        raise ValueError("invalid_history_shape")

    history = [item for item in history_raw if isinstance(item, dict)]
    snapshot = {
        "session_id": session_id,
        "metadata": {
            "title": _resolve_session_title(session_dir, history),
            "exported_at": exported_at,
            "filename": filename,
        },
        "records": history_raw,
    }
    return snapshot, filename
