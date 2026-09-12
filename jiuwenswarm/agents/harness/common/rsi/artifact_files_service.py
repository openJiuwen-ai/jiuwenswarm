# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""RSI 产物文件浏览服务（目录树 + 单文件读取）。"""

from __future__ import annotations

import base64
import hashlib
import mimetypes
import re
import shutil
import threading
import zipfile
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any

from jiuwenswarm.agents.harness.common.rsi.errors import (
    RsiArtifactNotFound,
    RsiBadRequest,
    RsiPathInvalid,
    RsiTaskNotFound,
)


_MAX_PREVIEW_BYTES = 10 * 1024 * 1024
_MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
_ARCHIVE_VIEW_DIR = ".rsi_artifact_views"
_ARCHIVE_VIEW_LOCK = threading.RLock()
_TASK_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]*$")
# Task materialization keeps dataset, Harness refs, model YAML (which may
# contain a decrypted API key), and the hidden profile beside engine output.
# Only these top-level directories are public artifact surfaces.
_PUBLIC_TOP_LEVEL_DIRS = frozenset({"artifact", "run", "snapshots", _ARCHIVE_VIEW_DIR})
_TEXT_SUFFIXES = {
    ".bib", ".cfg", ".csv", ".css", ".diff", ".h", ".htm", ".html", ".ini",
    ".js", ".json", ".jsonl", ".jsx", ".log", ".markdown", ".md", ".patch",
    ".py", ".sh", ".sty", ".tex", ".toml", ".ts", ".tsx", ".tsv", ".txt",
    ".xml", ".yaml", ".yml",
}


class RsiArtifactFilesService:
    """按 RSI tasks 根目录约束读取节点产物目录和文件。"""

    def __init__(self, store: Any) -> None:
        self.store = store

    def list_files(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")

        target = self._resolve_path(params.get("path"), task_id)
        if not target.exists():
            raise RsiArtifactNotFound(f"产物路径不存在: {target}")
        if target.is_file() and target.suffix.lower() == ".zip":
            root = self._materialize_archive(target, task_id)
            initial_path = self._select_initial_file(root)
            candidates = root.rglob("*")
        elif target.is_dir():
            root = target
            initial_path = self._select_initial_file(root)
            candidates = root.rglob("*")
        else:
            # A node-level ref may point directly to one file.  Do not expose
            # all of its unrelated siblings as if they belonged to the node.
            root = target.parent
            initial_path = target
            candidates = iter((target,))

        files: list[dict[str, Any]] = []
        for item in candidates:
            if not _is_public_path(item, self._resolve_task_root(task_id)):
                continue
            is_nested_archive_cache = (
                _ARCHIVE_VIEW_DIR in item.parts
                and root.parent.name != _ARCHIVE_VIEW_DIR
            )
            if "__MACOSX" in item.parts or is_nested_archive_cache:
                continue
            files.append(
                {
                    "name": item.name,
                    "path": str(item),
                    "isDirectory": item.is_dir(),
                    "size": item.stat().st_size if item.is_file() else 0,
                    "type": self._mime_type(item),
                }
            )
        files.sort(key=lambda item: (not item["isDirectory"], item["name"].lower()))
        return {
            "root": str(root),
            "initial_path": str(initial_path) if initial_path is not None else None,
            "files": files,
        }

    def read_file(self, params: dict[str, Any]) -> dict[str, Any]:
        task_id = str(params.get("task_id") or "").strip()
        if not task_id:
            raise RsiBadRequest("task_id 必填")

        target = self._resolve_path(params.get("path"), task_id)
        if not target.is_file():
            raise RsiArtifactNotFound(f"产物文件不存在: {target}")
        size = target.stat().st_size
        if size > _MAX_PREVIEW_BYTES:
            raise RsiBadRequest("产物文件超出预览大小限制")

        content_bytes = target.read_bytes()
        if target.suffix.lower() in _TEXT_SUFFIXES:
            content = content_bytes.decode("utf-8", errors="replace")
            encoding = "text"
        else:
            content = base64.b64encode(content_bytes).decode("ascii")
            encoding = "base64"
        return {
            "path": str(target),
            "name": target.name,
            "size": size,
            "type": self._mime_type(target),
            "encoding": encoding,
            "content": content,
        }

    def _resolve_path(self, raw_path: Any, task_id: str) -> Path:
        path_value = str(raw_path or "").strip()
        if not path_value:
            raise RsiBadRequest("path 必填")
        task_root = self._resolve_task_root(task_id)
        candidate = Path(path_value).expanduser()
        if not candidate.is_absolute():
            candidate = task_root / candidate
        candidate = candidate.resolve()
        if candidate == task_root:
            return candidate
        try:
            candidate.relative_to(task_root)
        except ValueError as exc:
            raise RsiPathInvalid("产物路径超出当前任务目录") from exc
        if not _is_public_path(candidate, task_root):
            raise RsiPathInvalid("产物路径不在公开产物目录")
        return candidate

    def _resolve_task_root(self, task_id: str) -> Path:
        if not _TASK_ID_RE.fullmatch(task_id):
            raise RsiTaskNotFound(task_id)
        tasks_root = Path(self.store.tasks_root).resolve()
        task_root = tasks_root / task_id
        if task_root.is_symlink() or not (task_root / "task.json").is_file():
            raise RsiTaskNotFound(task_id)
        resolved_root = task_root.resolve()
        try:
            resolved_root.relative_to(tasks_root)
        except ValueError as exc:
            raise RsiTaskNotFound(task_id) from exc
        return resolved_root

    def _materialize_archive(self, archive_path: Path, task_id: str) -> Path:
        """Extract a provider zip into a task-local, reusable preview cache."""

        task_root = self._resolve_task_root(task_id)
        cache_root = task_root / _ARCHIVE_VIEW_DIR
        cache_root.mkdir(parents=True, exist_ok=True)
        stat = archive_path.stat()
        digest = hashlib.sha256(
            f"{archive_path.resolve()}:{stat.st_size}:{stat.st_mtime_ns}".encode("utf-8")
        ).hexdigest()[:24]
        view_root = cache_root / digest
        marker = cache_root / f"{digest}.ready"
        with _ARCHIVE_VIEW_LOCK:
            if view_root.is_dir() and marker.is_file():
                return view_root

            temporary_root = cache_root / f".{digest}.tmp-{threading.get_ident()}"
            shutil.rmtree(temporary_root, ignore_errors=True)
            temporary_root.mkdir(parents=True, exist_ok=True)
            total_size = 0
            try:
                with zipfile.ZipFile(archive_path) as package:
                    for info in package.infolist():
                        member_name = str(info.filename).replace("\\", "/")
                        member = PurePosixPath(member_name)
                        if member.is_absolute() or ".." in member.parts:
                            raise RsiPathInvalid("产物压缩包包含非法路径")
                        parts = tuple(part for part in member.parts if part not in ("", "."))
                        if not parts:
                            continue
                        file_mode = (info.external_attr >> 16) & 0o170000
                        if file_mode == 0o120000:
                            raise RsiPathInvalid("产物压缩包不允许包含符号链接")
                        target = (temporary_root.joinpath(*parts)).resolve()
                        try:
                            target.relative_to(temporary_root.resolve())
                        except ValueError as exc:
                            raise RsiPathInvalid("产物压缩包包含非法路径") from exc
                        if info.is_dir():
                            target.mkdir(parents=True, exist_ok=True)
                            continue
                        total_size += max(0, int(info.file_size))
                        if total_size > _MAX_ARCHIVE_BYTES:
                            raise RsiBadRequest("产物压缩包超出预览大小限制")
                        target.parent.mkdir(parents=True, exist_ok=True)
                        with package.open(info) as source, target.open("wb") as destination:
                            shutil.copyfileobj(source, destination, length=1024 * 1024)
            except (RsiBadRequest, RsiPathInvalid):
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise
            except (OSError, zipfile.BadZipFile) as exc:
                shutil.rmtree(temporary_root, ignore_errors=True)
                raise RsiArtifactNotFound(f"产物压缩包无法读取: {archive_path.name}") from exc

            shutil.rmtree(view_root, ignore_errors=True)
            temporary_root.replace(view_root)
            marker.write_text("ready\n", encoding="utf-8")
            return view_root

    @classmethod
    def _select_initial_file(cls, root: Path) -> Path | None:
        preferred_names = (
            "main.pdf",
            "main.tex",
            "paper.pdf",
            "paper.tex",
            "report.md",
            "research_summary.md",
            "experiment_design.md",
            "README.md",
            "rsi_artifact_manifest.json",
        )
        files = [
            item
            for item in root.rglob("*")
            if item.is_file()
            and "__MACOSX" not in item.parts
            and "__rsi_artifact__" not in item.parts
        ]
        if not files:
            files = [
                item
                for item in root.rglob("*")
                if item.is_file() and "__MACOSX" not in item.parts
            ]
        if not files:
            return None
        by_name: dict[str, list[Path]] = {}
        for item in files:
            by_name.setdefault(item.name.lower(), []).append(item)
        for name in preferred_names:
            if name.lower() in by_name:
                return min(by_name[name.lower()], key=lambda item: cls._initial_file_sort_key(item, root))
        return min(files, key=lambda item: cls._initial_file_sort_key(item, root))

    @staticmethod
    def _initial_file_sort_key(path: Path, root: Path) -> tuple[int, str]:
        """Prefer generated paper output over copied input/original files."""

        relative = path.relative_to(root).as_posix().lower()
        priority = 0
        if "patched_paper" in relative or "/final/" in f"/{relative}/":
            priority -= 100
        if "/output/" in f"/{relative}/":
            priority -= 20
        if "/input/" in f"/{relative}/" or "original" in relative:
            priority += 40
        return priority, relative

    @staticmethod
    def _mime_type(path: Path) -> str:
        suffix = path.suffix.lower()
        if suffix == ".jsonl":
            return "application/x-ndjson"
        if suffix in {".diff", ".log", ".patch", ".out"}:
            return "text/plain"
        if suffix == ".csv":
            return "text/csv"
        return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _is_public_path(path: Path, task_root: Path) -> bool:
    """Return whether *path* belongs to a public artifact subtree."""

    try:
        relative = path.resolve().relative_to(task_root.resolve())
    except ValueError:
        return False
    return bool(relative.parts) and relative.parts[0] in _PUBLIC_TOP_LEVEL_DIRS
