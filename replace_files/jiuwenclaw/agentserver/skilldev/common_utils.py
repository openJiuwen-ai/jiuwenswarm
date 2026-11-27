"""Shared utility helpers for SkillDev."""

from __future__ import annotations

import fnmatch
import logging
import re
import shutil
import zipfile
from pathlib import Path

from jiuwenclaw.agentserver.skilldev.utils.archive_security import (
    validate_zip_archive_before_extract,
)

logger = logging.getLogger(__name__)

# 打包排除规则（与 PackageStageHandler 保持一致）
_EXCLUDE_DIRS = {"__pycache__", "node_modules", ".git", ".trash"}
_EXCLUDE_FILES = {".DS_Store"}
_EXCLUDE_GLOBS = {"*.pyc"}
_ROOT_EXCLUDE_DIRS = {"evals"}
_CLEANUP_DIRS = {"__pycache__"}
_CLEANUP_GLOBS = ("*.pyc", "*.pyo", "*.pyd")


def strip_agent_output_noise(text: str) -> str:
    """Remove leaked reasoning blocks and unexecuted text tool calls from agent output.

    Same rules as the former inline cleaners in evaluate_stage / test_run_stage_runner
    (superset: orphan closing tags, unclosed tool_call).
    """
    text = re.sub(r"<think>[\s\S]*?</think>", "", text)
    text = re.sub(r"</think>", "", text)
    text = re.sub(r"<think>", "", text)
    text = re.sub(r"<tool_call>[\s\S]*?</tool_call>", "", text)
    text = re.sub(r"<tool_call>[\s\S]*$", "", text)
    text = re.sub(r"</tool_call>[\s\S]*$", "", text)
    return text.strip()


def safe_extract_zip(
    zip_path: Path,
    dest_dir: Path,
    *,
    extract_to_stem_dir: bool = True,
) -> Path:
    """Extract a zip archive safely and return extraction directory.

    - Validates zip format.
    - Skips macOS metadata entries.
    - Prevents zip-slip path traversal.
    """
    if not zipfile.is_zipfile(zip_path):
        raise ValueError(f"文件不是合法的 zip: {zip_path.name}")

    validate_zip_archive_before_extract(zip_path)

    extract_dir = dest_dir / zip_path.stem if extract_to_stem_dir else dest_dir
    extract_dir.mkdir(parents=True, exist_ok=True)
    root = extract_dir.resolve()

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.infolist():
            name = member.filename
            if name.startswith("__MACOSX/") or name.startswith("._"):
                continue
            target = (extract_dir / name).resolve()
            if not str(target).startswith(str(root)):
                continue
            zf.extract(member, extract_dir)

    return extract_dir


def repack_skill_dir(
    skill_dir: Path, output_dir: Path, session_id: str = ""
) -> tuple[Path, str | None]:
    """删除 output_dir 下旧的 .skill/.zip 文件，重新将 skill_dir 打包.

    支持两种目录结构：
    - skill_dir/SKILL.md（平铺）
    - skill_dir/<subdir>/SKILL.md（嵌套一层）

    嵌套情况下，如果 SKILL.md 中解析出的 name 与子目录名不一致，会重命名子目录。

    Returns:
        (zip_path, renamed_to) - zip_path 为新生成的压缩包路径，
        renamed_to 为重命名后的目录名（未重命名时为 None）。
    """
    from jiuwenclaw.agentserver.skilldev.stages.validate_stage import parse_skill_frontmatter

    output_dir.mkdir(exist_ok=True)

    for old in output_dir.iterdir():
        if old.suffix in (".zip", ".skill"):
            old.unlink()
            logger.debug("[session=%s] [repack] 已删除旧包: %s", session_id, old.name)

    skill_md_path, pack_root = _locate_skill_md(skill_dir)
    skill_name, _, _ = parse_skill_frontmatter(skill_md_path)

    # 嵌套结构下：子目录名与 skill_name 不一致时重命名
    renamed_to: str | None = None
    if skill_name and pack_root != skill_dir and pack_root.name != skill_name:
        new_pack_root = pack_root.parent / skill_name
        if new_pack_root.exists():
            shutil.rmtree(new_pack_root)
        pack_root.rename(new_pack_root)
        pack_root = new_pack_root
        renamed_to = skill_name
        logger.info(
            "[session=%s] [repack] 子目录已重命名为: %s", session_id, skill_name
        )

    _cleanup_executables(pack_root, session_id)

    skill_filename = f"{skill_name}.zip"
    zip_path = output_dir / skill_filename

    _zip_skill_dir(pack_root, zip_path, skill_name, session_id)
    return zip_path, renamed_to


def _locate_skill_md(skill_dir: Path) -> tuple[Path, Path]:
    """定位 SKILL.md 文件，返回 (skill_md_path, pack_root).

    pack_root 是实际需要打包的根目录。
    """
    # 情况1: skill_dir/SKILL.md
    direct = skill_dir / "SKILL.md"
    if direct.exists():
        return direct, skill_dir

    # 情况2: skill_dir/<subdir>/SKILL.md（仅查找一层子目录）
    for child in skill_dir.iterdir():
        if child.is_dir():
            nested = child / "SKILL.md"
            if nested.exists():
                return nested, child

    raise FileNotFoundError(
        f"在 {skill_dir} 中未找到 SKILL.md（平铺或一层嵌套均未发现）"
    )


def _cleanup_executables(skill_dir: Path, session_id: str) -> None:
    """递归清理 skill_dir 中的 pyc 等可执行/编译产物."""
    if not skill_dir.exists() or not skill_dir.is_dir():
        return

    for pattern in _CLEANUP_GLOBS:
        for file_path in skill_dir.rglob(pattern):
            if file_path.is_file():
                try:
                    file_path.unlink()
                except OSError:
                    pass

    for dir_name in _CLEANUP_DIRS:
        for dir_path in list(skill_dir.rglob(dir_name)):
            if dir_path.is_dir():
                try:
                    shutil.rmtree(dir_path)
                except OSError:
                    pass


def _zip_skill_dir(
    skill_dir: Path, zip_path: Path, root_dir_name: str, session_id: str
) -> None:
    """将 skill_dir 打包为 zip，排除无关文件并添加根目录."""
    with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for file_path in skill_dir.rglob("*"):
            if not file_path.is_file():
                continue
            if _should_exclude(file_path, skill_dir):
                continue
            arcname = Path(root_dir_name) / file_path.relative_to(skill_dir)
            zf.write(file_path, arcname)
    logger.info(
        "[session=%s] [repack] 打包完成: %s (%d bytes)",
        session_id,
        zip_path.name,
        zip_path.stat().st_size,
    )


def _should_exclude(file_path: Path, skill_dir: Path) -> bool:
    """判断文件是否应被排除出 zip 包."""
    rel_path = file_path.relative_to(skill_dir)
    parts = rel_path.parts

    if any(part in _EXCLUDE_DIRS for part in parts):
        return True
    if len(parts) > 0 and parts[0] in _ROOT_EXCLUDE_DIRS:
        return True
    if rel_path.name in _EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(rel_path.name, pat) for pat in _EXCLUDE_GLOBS)
