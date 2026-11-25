"""Theme import, backup export and restore (DESIGN.md section 4.4).

The host hands an import over as ``.uploads/<id>`` inside the data directory and deletes it when the
handler returns, so the handler only moves it to ``incoming/``; everything else here runs in a
worker thread afterwards. ZIP handling reuses the V1 guards from ``archive.py``.
"""

from __future__ import annotations

import json
import shutil
import stat
import tempfile
import time
import zipfile
from pathlib import Path, PurePosixPath
from typing import Any
from uuid import uuid4

from . import ENGINE_VERSION
from .archive import MAX_ARCHIVE_BYTES, MAX_ENTRY_RATIO, _entry_parts, extract_theme_archive
from .catalog import THEMES_DIR, Catalog
from .convert import convert_pack
from .schema import MANIFEST_NAME, MAX_FILE_BYTES, MAX_PACK_BYTES, ThemePack, ThemeValidationError, load_theme_pack

INCOMING_DIR = "incoming"
EXPORTS_DIR = "exports"
BACKUP_FILE = "backup.json"
BACKUP_FORMAT = "workswarm-theme-backup"
BACKUP_NAME = "WorkSwarm 主题背景备份.zip"
MAX_BACKUP_FILES = 4000


def stage_upload(data_root: Path, descriptor: dict[str, Any]) -> Path:
    """Move the host's upload directory out of its reach; a rename on one disk is instant."""
    source = (data_root / descriptor["path"]).resolve()
    if not source.is_relative_to(data_root.resolve()) or not source.is_dir():
        raise ThemeValidationError(["导入的文件不可用"])
    target = data_root / INCOMING_DIR / descriptor["upload_id"]
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return target


def _theme_root(folder: Path) -> Path:
    """The folder holding theme.json: the upload itself or its only sub-folder."""
    current = folder
    for _ in range(2):
        if (current / MANIFEST_NAME).is_file():
            return current
        children = [child for child in current.iterdir() if not child.name.startswith(".")]
        if len(children) != 1 or not children[0].is_dir():
            break
        current = children[0]
    raise ThemeValidationError([f"文件夹根目录（或唯一的子文件夹）中必须包含 {MANIFEST_NAME}"])


def install_theme(source: Path, data_root: Path, reserved: set[str]) -> tuple[ThemePack, list[str]]:
    """Validate, convert and publish one theme folder; replaces older versions of the same id."""
    pack = load_theme_pack(source, source="imported")
    if pack.id in reserved:
        raise ThemeValidationError([f"内置主题已使用 ID {pack.id}，导入的主题不能覆盖它"])
    themes = data_root / "public" / THEMES_DIR / pack.id
    staging = themes / f".staging-{uuid4().hex}"
    try:
        notes = convert_pack(pack, staging)
        final = themes / pack.version
        if final.exists():
            shutil.rmtree(final)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    for other in themes.iterdir():
        if other.is_dir() and other != final:
            shutil.rmtree(other, ignore_errors=True)
    return load_theme_pack(final, source="imported"), notes + pack.warnings


def import_staged(staged: Path, data_root: Path, reserved: set[str]) -> tuple[ThemePack, list[str]]:
    """Import one staged upload: a single ZIP or a folder."""
    try:
        entries = [path for path in staged.rglob("*") if path.is_file()]
        if len(entries) == 1 and entries[0].suffix.lower() == ".zip" and entries[0].parent == staged:
            with tempfile.TemporaryDirectory(prefix=".extract-", dir=staged) as workdir:
                return install_theme(extract_theme_archive(entries[0], Path(workdir)), data_root, reserved)
        return install_theme(_theme_root(staged), data_root, reserved)
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def remove_theme(data_root: Path, theme_id: str) -> None:
    target = data_root / "public" / THEMES_DIR / theme_id
    if target.is_dir() and not target.is_symlink():
        shutil.rmtree(target)


# ── backup ──────────────────────────────────────────────────────────────


def build_backup(data_root: Path, prefs: dict[str, Any], catalog: Catalog) -> str:
    """Write the backup ZIP under ``exports/`` and return its path relative to the data directory."""
    exports = data_root / EXPORTS_DIR
    if exports.exists():
        shutil.rmtree(exports)
    exports.mkdir(parents=True)
    imported = [theme for theme in catalog.ordered() if theme.source == "imported"]
    summary = {
        "format": BACKUP_FORMAT, "version": 1, "engine": ENGINE_VERSION,
        "created_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "prefs": prefs, "themes": [{"id": theme.id, "version": theme.pack.version} for theme in imported],
    }
    target = exports / "backup.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(BACKUP_FILE, json.dumps(summary, ensure_ascii=False, indent=2))
        for theme in imported:
            for item in theme.pack.files:
                archive.write(theme.pack.root / item.path, f"{THEMES_DIR}/{theme.id}/{item.path}")
    return f"{EXPORTS_DIR}/backup.zip"


def _extract_backup(archive_path: Path, workdir: Path) -> Path:
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ThemeValidationError(["备份文件超过 200 MiB"])
    try:
        bundle = zipfile.ZipFile(archive_path)
    except zipfile.BadZipFile as exc:
        raise ThemeValidationError(["文件不是有效的 ZIP 压缩包"]) from exc
    with bundle:
        entries = []
        total = 0
        for info in bundle.infolist():
            if info.is_dir():
                continue
            if stat.S_ISLNK(info.external_attr >> 16) or info.flag_bits & 0x1:
                raise ThemeValidationError(["备份中不能包含符号链接或加密条目"])
            parts = _entry_parts(info)
            if parts is None:
                continue
            if info.file_size > MAX_FILE_BYTES or (
                    info.compress_size and info.file_size / info.compress_size > MAX_ENTRY_RATIO
                    and info.file_size > 1024 * 1024):
                raise ThemeValidationError([f"备份中的文件异常：{info.filename}"])
            total += info.file_size
            entries.append((info, parts))
        if len(entries) > MAX_BACKUP_FILES or total > MAX_PACK_BYTES:
            raise ThemeValidationError(["备份过大"])
        root = workdir / "backup"
        for info, parts in entries:
            target = root.joinpath(*parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with bundle.open(info) as source, open(target, "wb") as handle:
                shutil.copyfileobj(source, handle, 1024 * 1024)
        return root


def restore_staged(staged: Path, data_root: Path, reserved: set[str]) -> tuple[dict[str, Any], list[str], list[str]]:
    """Restore a staged backup ZIP. Returns (stored preferences, restored ids, messages)."""
    try:
        entries = [path for path in staged.rglob("*") if path.is_file()]
        if len(entries) != 1 or entries[0].suffix.lower() != ".zip":
            raise ThemeValidationError(["请选择导出的备份文件（.zip）"])
        with tempfile.TemporaryDirectory(prefix=".restore-", dir=staged) as workdir:
            root = _extract_backup(entries[0], Path(workdir))
            try:
                summary = json.loads((root / BACKUP_FILE).read_text(encoding="utf-8"))
            except (OSError, ValueError) as exc:
                raise ThemeValidationError(["备份中缺少有效的 backup.json"]) from exc
            if not isinstance(summary, dict) or summary.get("format") != BACKUP_FORMAT or summary.get("version") != 1:
                raise ThemeValidationError(["这不是主题背景的备份文件"])
            restored: list[str] = []
            messages: list[str] = []
            themes_dir = root / THEMES_DIR
            for folder in sorted(themes_dir.iterdir()) if themes_dir.is_dir() else []:
                if not folder.is_dir():
                    continue
                try:
                    pack, _ = install_theme(folder, data_root, reserved)
                    restored.append(pack.id)
                except ThemeValidationError as exc:
                    messages.append(f"{folder.name}：{'；'.join(exc.errors[:2])}")
            return summary.get("prefs") or {}, restored, messages
    finally:
        shutil.rmtree(staged, ignore_errors=True)


def page_relative(path: str) -> str:
    return PurePosixPath(path).as_posix()
