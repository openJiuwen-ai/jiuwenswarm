"""从 ZIP 导入主题包：先在临时目录完整校验，通过后再原子替换。"""

from __future__ import annotations

import shutil
import stat
import tempfile
import zipfile
from pathlib import Path, PurePosixPath

from .schema import (
    IGNORED_NAMES,
    MANIFEST_NAME,
    MAX_FILE_BYTES,
    MAX_PACK_BYTES,
    MAX_PACK_FILES,
    ThemePack,
    ThemeValidationError,
    file_kind,
    load_theme_pack,
)

MAX_ARCHIVE_BYTES = 200 * 1024 * 1024
MAX_ENTRY_RATIO = 200


def _entry_parts(info: zipfile.ZipInfo) -> tuple[str, ...] | None:
    name = info.filename
    if "\\" in name or name.startswith("/") or ":" in name or "\x00" in name:
        raise ThemeValidationError([f"压缩包含有不安全的路径：{name}"])
    parts = tuple(part for part in PurePosixPath(name).parts if part)
    if any(part in ("..", ".") for part in parts):
        raise ThemeValidationError([f"压缩包含有不安全的路径：{name}"])
    if not parts or parts[0] == "__MACOSX" or parts[-1] in IGNORED_NAMES:
        return None
    if any(part.startswith(".") for part in parts):
        return None
    return parts


def _find_root(names: list[tuple[str, ...]]) -> tuple[str, ...]:
    if (MANIFEST_NAME,) in names:
        return ()
    tops = {parts[0] for parts in names}
    if len(tops) == 1:
        top = next(iter(tops))
        if (top, MANIFEST_NAME) in names:
            return (top,)
    raise ThemeValidationError([f"压缩包根目录（或唯一的顶层文件夹）中必须包含 {MANIFEST_NAME}"])


def extract_theme_archive(archive: Path, workdir: Path) -> Path:
    """把 ZIP 安全解压到 ``workdir``，返回主题包根目录。"""

    if archive.stat().st_size > MAX_ARCHIVE_BYTES:
        raise ThemeValidationError(["压缩包超过 200 MiB"])
    try:
        bundle = zipfile.ZipFile(archive)
    except zipfile.BadZipFile as exc:
        raise ThemeValidationError(["文件不是有效的 ZIP 压缩包"]) from exc
    with bundle:
        entries: list[tuple[zipfile.ZipInfo, tuple[str, ...]]] = []
        for info in bundle.infolist():
            if info.is_dir():
                continue
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise ThemeValidationError([f"压缩包不能包含符号链接：{info.filename}"])
            if info.flag_bits & 0x1:
                raise ThemeValidationError(["不支持加密的压缩包"])
            parts = _entry_parts(info)
            if parts is not None:
                entries.append((info, parts))
        if not entries:
            raise ThemeValidationError(["压缩包是空的"])
        if len(entries) > MAX_PACK_FILES:
            raise ThemeValidationError([f"压缩包文件数超过 {MAX_PACK_FILES}"])
        root = _find_root([parts for _, parts in entries])

        total = 0
        rejected = []
        for info, parts in entries:
            if parts[: len(root)] != root:
                raise ThemeValidationError([f"文件不在主题包目录内：{info.filename}"])
            rel = PurePosixPath(*parts[len(root):]).as_posix()
            if file_kind(rel) is None:
                rejected.append(rel)
                continue
            if info.file_size > MAX_FILE_BYTES:
                raise ThemeValidationError([f"文件超过 64 MiB：{rel}"])
            if info.compress_size and info.file_size / info.compress_size > MAX_ENTRY_RATIO and info.file_size > 1024 * 1024:
                raise ThemeValidationError([f"文件压缩比异常，已拒绝：{rel}"])
            total += info.file_size
        if rejected:
            shown = "、".join(rejected[:6]) + ("……" if len(rejected) > 6 else "")
            raise ThemeValidationError([f"主题包中含有不支持的文件类型：{shown}"])
        if total > MAX_PACK_BYTES:
            raise ThemeValidationError(["解压后总大小超过 256 MiB"])

        target_root = workdir / "pack"
        target_root.mkdir(parents=True)
        for info, parts in entries:
            rel_parts = parts[len(root):]
            target = target_root.joinpath(*rel_parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            written = 0
            with bundle.open(info) as src, open(target, "wb") as dst:
                while chunk := src.read(1024 * 1024):
                    written += len(chunk)
                    if written > info.file_size or written > MAX_FILE_BYTES:
                        raise ThemeValidationError([f"文件实际大小与记录不符：{'/'.join(rel_parts)}"])
                    dst.write(chunk)
        return target_root


def import_theme_archive(
    archive: Path,
    packs_dir: Path,
    *,
    reserved_ids: set[str] = frozenset(),
) -> ThemePack:
    """校验并安装 ZIP 主题包到 ``packs_dir/<id>``。

    ``reserved_ids`` 是来自其他来源（扩展目录、内置）的主题 ID，导入包不能覆盖它们。
    """

    packs_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix=".import-", dir=packs_dir) as tmp:
        extracted = extract_theme_archive(Path(archive), Path(tmp))
        pack = load_theme_pack(extracted, source="imported")
        if pack.id in reserved_ids:
            raise ThemeValidationError([f"已存在同 ID 的主题包（{pack.id}），请先在原位置删除或修改 ID"])
        destination = packs_dir / pack.id
        staging = Path(tmp) / "staged"
        shutil.move(str(extracted), staging)
        backup = None
        if destination.exists():
            backup = Path(tmp) / "previous"
            shutil.move(str(destination), backup)
        try:
            shutil.move(str(staging), destination)
        except BaseException:
            if backup is not None and not destination.exists():
                shutil.move(str(backup), destination)
            raise
    return load_theme_pack(destination, source="imported")
