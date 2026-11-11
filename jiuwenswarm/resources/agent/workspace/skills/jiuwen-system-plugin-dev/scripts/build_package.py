"""Validate the target Core manifest and create a ZIP with no outer folder."""

from __future__ import annotations

import argparse
import json
import os
import sys
import tempfile
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

from openjiuwen.core.extension.manifest import MANIFEST_NAME, load_manifest

EXCLUDED_DIRECTORIES = frozenset({".git", ".venv", "venv", "node_modules", "__pycache__", ".pytest_cache"})
EXCLUDED_SUFFIXES = frozenset({".pyc", ".pyo"})


def package_files(source: Path) -> list[Path]:
    """Collect portable source files without caches or local environments."""
    files: list[Path] = []
    for current, directories, names in os.walk(source, followlinks=False):
        current_path = Path(current)
        kept: list[str] = []
        for name in sorted(directories):
            if name in EXCLUDED_DIRECTORIES:
                continue
            directory = current_path / name
            if directory.is_symlink():
                raise ValueError(f"系统插件中不能包含符号链接目录：{directory}")
            kept.append(name)
        directories[:] = kept
        for name in sorted(names):
            path = current_path / name
            if path.suffix.lower() in EXCLUDED_SUFFIXES:
                continue
            if path.is_symlink():
                raise ValueError(f"系统插件中不能包含符号链接：{path}")
            is_template = name.endswith((".example", ".sample", ".template"))
            if (name == ".env" or name.startswith(".env.")) and not is_template:
                raise ValueError(f"请移除本地环境配置后再打包：{path}")
            if not path.is_file() or not path.resolve().is_relative_to(source):
                raise ValueError(f"系统插件文件不是根目录内的普通文件：{path}")
            files.append(path)
    return sorted(files, key=lambda path: path.relative_to(source).as_posix())


def build_package(source: Path, output: Path, *, overwrite: bool = False) -> Path:
    """Validate without executing system plugin code, then atomically write a ZIP."""
    source = source.expanduser().resolve()
    output = output.expanduser().resolve()
    if not source.is_dir():
        raise ValueError(f"系统插件源码目录不存在：{source}")
    if output.suffix.lower() != ".zip":
        raise ValueError("输出文件必须使用 .zip 后缀")
    if output.is_relative_to(source):
        raise ValueError("输出 ZIP 请放在系统插件源码目录之外，避免将 ZIP 产物再次打包")
    if output.exists() and not overwrite:
        raise FileExistsError(f"输出已存在；确认替换时使用 --force：{output}")

    # 使用目标 Core 的实际解析器校验 ID、版本、声明依赖与入口路径；不执行 create_extension。
    manifest = load_manifest(source, require_domain=True)
    files = package_files(source)
    names = {path.relative_to(source).as_posix() for path in files}
    if MANIFEST_NAME not in names:
        raise ValueError(f"系统插件根目录必须包含 {MANIFEST_NAME}")
    for extension in manifest.extensions:
        # 与 ZIP 的路径格式一致，防止入口落在被排除的缓存或环境目录中。
        entry = Path(extension.module).as_posix()
        if entry not in names:
            raise ValueError(f"代码入口未包含在 ZIP 中：{extension.module}")

    output.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(prefix=".extension-package-", suffix=".zip", dir=output.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with ZipFile(temporary, "w", compression=ZIP_DEFLATED) as archive:
            for path in files:
                # 固定元信息，确保相同源码生成相同字节；ZIP 根目录直接是系统插件内容。
                entry = ZipInfo(path.relative_to(source).as_posix(), date_time=(2020, 1, 1, 0, 0, 0))
                entry.compress_type = ZIP_DEFLATED
                entry.external_attr = 0o644 << 16
                archive.writestr(entry, path.read_bytes())
        with ZipFile(temporary) as archive:
            if archive.testzip() is not None:
                raise ValueError("ZIP 完整性校验失败")
        os.replace(temporary, output)
    finally:
        if temporary.exists():
            temporary.unlink()
    return output


def main() -> int:
    # 终端默认编码因平台而异；客户端输出统一使用 UTF-8，保留中文路径和错误说明。
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="校验系统插件 manifest，生成可安装的 ZIP")
    parser.add_argument("source", type=Path, help=f"根目录包含 {MANIFEST_NAME} 的源码目录")
    parser.add_argument("output", type=Path, help="源码目录之外的输出 ZIP")
    parser.add_argument("--force", action="store_true", help="覆盖已有输出文件")
    arguments = parser.parse_args()
    try:
        result = build_package(arguments.source, arguments.output, overwrite=arguments.force)
    except Exception as error:
        parser.exit(1, f"打包失败：{error}\n")
    print(json.dumps({"zip": str(result), "validated": ["core_manifest", "entry_paths", "zip_layout"]},
                     ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
