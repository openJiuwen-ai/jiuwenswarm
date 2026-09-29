# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""工作区磁盘操作：列目录、删除、预览、下载、全量 du。"""

from __future__ import annotations

import io
import logging
import mimetypes
import os
import shutil
import subprocess
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from jiuwenswarm.common.workspace.zones import (
    classify_zone,
    is_deletable,
    normalize_relative_path,
)

logger = logging.getLogger(__name__)

# 目录删除安全上限（设计：限深限量；未给精确数字时取保守值）。
_DEFAULT_MAX_DELETE_DEPTH = 32
_DEFAULT_MAX_DELETE_ENTRIES = 5000
_DEFAULT_PREVIEW_MAX_BYTES = 65536
_DEFAULT_DOWNLOAD_MAX_BYTES = 100 * 1024 * 1024


class WorkspaceError(Exception):
    """业务错误；``code`` 对齐 Web HTTP ``error.code``。"""

    def __init__(self, code: str, message: str, *, details: dict[str, Any] | None = None):
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}


@dataclass(frozen=True)
class WorkspaceService:
    """对单一租户根目录提供 list / delete / preview / download / du。"""

    tenant_root: Path
    max_delete_depth: int = _DEFAULT_MAX_DELETE_DEPTH
    max_delete_entries: int = _DEFAULT_MAX_DELETE_ENTRIES
    max_download_bytes: int = _DEFAULT_DOWNLOAD_MAX_BYTES

    def resolve(self, relative_path: str | None) -> Path:
        """Resolve relative path under tenant root; raise on traversal / escape."""
        try:
            rel = normalize_relative_path(relative_path)
        except ValueError as exc:
            raise WorkspaceError("BAD_REQUEST", "invalid relative_path") from exc
        root = self.tenant_root.resolve()
        if not rel:
            return root
        candidate = (root / rel).resolve()
        try:
            if os.path.commonpath([str(root), str(candidate)]) != str(root):
                raise WorkspaceError("FORBIDDEN", "path escapes tenant root")
        except ValueError as exc:
            raise WorkspaceError("FORBIDDEN", "path escapes tenant root") from exc
        return candidate

    def list_tree(self, relative_path: str | None = None) -> dict[str, Any]:
        try:
            rel = normalize_relative_path(relative_path)
        except ValueError as exc:
            raise WorkspaceError("BAD_REQUEST", "invalid relative_path") from exc
        target = self.resolve(rel)
        if not target.exists():
            # 根目录尚未物化：返回空列表，避免前端整页失败；子路径仍 404。
            if not rel:
                return {"relative_path": rel, "entries": []}
            raise WorkspaceError("NOT_FOUND", "path not found")
        if not target.is_dir():
            raise WorkspaceError("BAD_REQUEST", "relative_path is not a directory")

        entries: list[dict[str, Any]] = []
        try:
            children = sorted(
                target.iterdir(),
                key=lambda p: (not p.is_dir(), p.name.lower()),
            )
        except OSError as exc:
            raise WorkspaceError("INTERNAL_ERROR", str(exc)) from exc

        for child in children:
            child_rel = f"{rel}/{child.name}" if rel else child.name
            child_rel = child_rel.replace("\\", "/")
            zone = classify_zone(child_rel)
            try:
                st = child.stat()
            except OSError:
                continue
            is_dir = child.is_dir()
            size: int | None
            if is_dir:
                size = None  # 可选懒加载；P0 目录占用为 null
            else:
                size = int(st.st_size)
            mime_hint: str | None = None
            if not is_dir:
                guessed, _ = mimetypes.guess_type(child.name)
                mime_hint = guessed
            entries.append(
                {
                    "name": child.name,
                    "relative_path": child_rel,
                    "is_dir": is_dir,
                    "size_bytes": size,
                    "mtime_ms": int(st.st_mtime * 1000),
                    "ctime_ms": int(getattr(st, "st_ctime", st.st_mtime) * 1000),
                    "zone": zone.value,
                    "deletable": is_deletable(child_rel),
                    "mime_hint": mime_hint,
                }
            )

        return {"relative_path": rel, "entries": entries}

    def delete_entries(self, relative_paths: list[str]) -> dict[str, Any]:
        results: list[dict[str, Any]] = []
        for raw in relative_paths or []:
            results.append(self._delete_one(str(raw)))
        return {"results": results}

    def _delete_one(self, relative_path: str) -> dict[str, Any]:
        try:
            rel = normalize_relative_path(relative_path)
        except ValueError:
            return {"relative_path": relative_path, "ok": False, "error": "bad_path"}
        if not rel:
            return {"relative_path": relative_path, "ok": False, "error": "forbidden_zone"}
        if not is_deletable(rel):
            return {"relative_path": rel, "ok": False, "error": "forbidden_zone"}
        target = self.resolve(rel)
        if not target.exists():
            return {"relative_path": rel, "ok": False, "error": "not_found"}
        try:
            if target.is_dir():
                self._assert_dir_delete_budget(target)
                shutil.rmtree(target)
            else:
                target.unlink()
        except WorkspaceError as exc:
            return {"relative_path": rel, "ok": False, "error": exc.message}
        except OSError as exc:
            logger.warning("[workspace] delete failed path=%s err=%s", rel, exc)
            return {"relative_path": rel, "ok": False, "error": "io_error"}
        return {"relative_path": rel, "ok": True, "error": None}

    def _assert_dir_delete_budget(self, root: Path) -> None:
        depth_cap = max(1, int(self.max_delete_depth))
        entry_cap = max(1, int(self.max_delete_entries))
        count = 0
        for dirpath, dirnames, filenames in os.walk(root):
            rel_depth = Path(dirpath).resolve().relative_to(root.resolve()).parts
            if len(rel_depth) >= depth_cap:
                raise WorkspaceError("BAD_REQUEST", "delete_too_deep")
            count += len(dirnames) + len(filenames)
            if count > entry_cap:
                raise WorkspaceError("BAD_REQUEST", "delete_too_many")

    def preview_file(
        self,
        relative_path: str | None,
        *,
        max_bytes: int | None = None,
    ) -> dict[str, Any]:
        """预览文件：文本返回截断正文；二进制 ``content=null``。目录 → 400。"""
        try:
            rel = normalize_relative_path(relative_path)
        except ValueError as exc:
            raise WorkspaceError("BAD_REQUEST", "invalid relative_path") from exc
        if not rel:
            raise WorkspaceError("BAD_REQUEST", "relative_path required")
        target = self.resolve(rel)
        if not target.exists():
            raise WorkspaceError("NOT_FOUND", "path not found")
        if target.is_dir():
            raise WorkspaceError("BAD_REQUEST", "path is a directory")
        if not target.is_file():
            raise WorkspaceError("BAD_REQUEST", "path is not a file")

        limit = int(max_bytes) if max_bytes is not None else _DEFAULT_PREVIEW_MAX_BYTES
        if limit <= 0:
            limit = _DEFAULT_PREVIEW_MAX_BYTES
        try:
            # 只读前 limit(+1) 字节，避免超大文件整文件进内存导致 OOM。
            with open(target, "rb") as handle:
                sample = handle.read(limit + 1)
        except OSError as exc:
            raise WorkspaceError("INTERNAL_ERROR", str(exc)) from exc

        truncated = len(sample) > limit
        sample = sample[:limit]
        if _looks_binary(sample):
            return {
                "relative_path": rel,
                "truncated": truncated,
                "content": None,
            }
        try:
            text = sample.decode("utf-8")
        except UnicodeDecodeError:
            try:
                text = sample.decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                return {
                    "relative_path": rel,
                    "truncated": truncated,
                    "content": None,
                }
        return {
            "relative_path": rel,
            "truncated": truncated,
            "content": text,
        }

    def download_payload(self, relative_path: str | None) -> dict[str, Any]:
        """准备下载：文件原样，目录打 zip；超限 ``download_too_large``。

        Returns:
            ``filename`` / ``content_type`` / ``content`` (bytes)
        """
        try:
            rel = normalize_relative_path(relative_path)
        except ValueError as exc:
            raise WorkspaceError("BAD_REQUEST", "invalid relative_path") from exc
        if not rel:
            raise WorkspaceError("BAD_REQUEST", "relative_path required")
        target = self.resolve(rel)
        if not target.exists():
            raise WorkspaceError("NOT_FOUND", "path not found")

        max_bytes = max(1, int(self.max_download_bytes))
        if target.is_file():
            try:
                size = target.stat().st_size
            except OSError as exc:
                raise WorkspaceError("INTERNAL_ERROR", str(exc)) from exc
            if size > max_bytes:
                raise WorkspaceError("BAD_REQUEST", "download_too_large")
            try:
                data = target.read_bytes()
            except OSError as exc:
                raise WorkspaceError("INTERNAL_ERROR", str(exc)) from exc
            mime, _ = mimetypes.guess_type(target.name)
            return {
                "filename": target.name,
                "content_type": mime or "application/octet-stream",
                "content": data,
            }

        if not target.is_dir():
            raise WorkspaceError("BAD_REQUEST", "path is not a file or directory")

        buf = io.BytesIO()
        total = 0
        try:
            with zipfile.ZipFile(buf, mode="w", compression=zipfile.ZIP_DEFLATED) as zf:
                # followlinks=False：不进入符号链接目录；文件符号链接也必须跳过，
                # 否则 ZipFile.write 会跟随目标，可能把租户外敏感文件打进包。
                for dirpath, dirnames, filenames in os.walk(target, followlinks=False):
                    dirnames[:] = [
                        name
                        for name in dirnames
                        if not (Path(dirpath) / name).is_symlink()
                    ]
                    for name in filenames:
                        full = Path(dirpath) / name
                        if full.is_symlink():
                            continue
                        try:
                            st = full.stat()
                        except OSError:
                            continue
                        total += int(st.st_size)
                        if total > max_bytes:
                            raise WorkspaceError("BAD_REQUEST", "download_too_large")
                        arcname = full.relative_to(target).as_posix()
                        zf.write(full, arcname)
        except OSError as exc:
            raise WorkspaceError("INTERNAL_ERROR", str(exc)) from exc

        data = buf.getvalue()
        if len(data) > max_bytes:
            raise WorkspaceError("BAD_REQUEST", "download_too_large")
        return {
            "filename": f"{target.name}.zip",
            "content_type": "application/zip",
            "content": data,
        }

    def measure_used_bytes(self) -> int:
        """Full ``du -s -B1`` on tenant root; fallback to walk on non-Unix."""
        root = self.tenant_root.resolve()
        if not root.exists():
            return 0
        return measure_used_bytes(root)


def _looks_binary(sample: bytes) -> bool:
    if not sample:
        return False
    if b"\x00" in sample:
        return True
    # 高比例不可打印控制字符（保留常见空白）
    textish = 0
    for b in sample:
        if b in (9, 10, 13) or 32 <= b <= 126 or b >= 0x80:
            textish += 1
    return (textish / len(sample)) < 0.85


def measure_used_bytes(tenant_root: Path) -> int:
    """Run ``du -s -B1`` when available; otherwise walk files."""
    root = Path(tenant_root).resolve()
    if not root.exists():
        return 0
    try:
        completed = subprocess.run(
            ["/usr/bin/du", "-s", "-B1", str(root)],
            check=False,
            capture_output=True,
            text=True,
            timeout=120,
        )
        if completed.returncode == 0 and completed.stdout.strip():
            first = completed.stdout.strip().splitlines()[0]
            token = first.split()[0].strip()
            return max(0, int(token))
    except (OSError, ValueError, subprocess.TimeoutExpired) as exc:
        logger.debug("[workspace] du unavailable, walk fallback: %s", exc)

    total = 0
    for dirpath, _dirnames, filenames in os.walk(root):
        for name in filenames:
            try:
                total += (Path(dirpath) / name).stat().st_size
            except OSError:
                continue
    return total


def list_tree(tenant_root: Path, relative_path: str | None = None) -> dict[str, Any]:
    return WorkspaceService(tenant_root=Path(tenant_root)).list_tree(relative_path)


def delete_entries(tenant_root: Path, relative_paths: list[str]) -> dict[str, Any]:
    return WorkspaceService(tenant_root=Path(tenant_root)).delete_entries(relative_paths)
