# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""directImport 流程：解压上传包、校验 SKILL.md、打包。"""

from __future__ import annotations

import base64
import fnmatch
import logging
import re
import shutil
import zipfile
from pathlib import Path
from typing import Any

import yaml

from jiuwenclaw.agentserver.skilldev.common_utils import safe_extract_zip
from jiuwenclaw.agentserver.skilldev.utils.download_file_from_url import download_file
from jiuwenclaw.agentserver.skilldev.utils.skill_md_validation import (
    BODY_MAX_LINES,
    BODY_MAX_TOKENS,
    DESCRIPTION_MAX_CHARS_CJK,
    DESCRIPTION_MAX_CHARS_EN,
    DESCRIPTION_MAX_TOKENS,
    DESCRIPTION_MAX_WEIGHTED,
    description_weighted_len,
    estimate_skill_tokens,
)
from jiuwenclaw.agentserver.skilldev.utils.skill_description_fix import (
    normalize_skill_description,
    parse_frontmatter,
)

logger = logging.getLogger(__name__)

SKILL_NAME_PATTERN = re.compile(r"^[a-z0-9-]{1,64}$")

EXCLUDE_DIRS = {"__pycache__", "node_modules", ".trash"}
EXCLUDE_GLOBS = {"*.pyc", "*.swp"}
EXCLUDE_FILES = {".DS_Store"}
ROOT_EXCLUDE_DIRS = {"evals", "output"}


def extract_import_url(params: dict[str, Any]) -> str | None:
    """从 directImport 的 skill 包条目中提取 url（files / skill_packages，用于安全扫描）。"""

    def _url_from_package(item: dict[str, Any]) -> str | None:
        name = str(item.get("filename") or item.get("name") or "").strip()
        if Path(name).suffix.lower() not in (".zip", ".skill"):
            return None
        raw = item.get("url")
        if raw is None:
            return None
        value = str(raw).strip()
        return value or None

    for item in params.get("skill_packages") or params.get("skillPackages") or []:
        if isinstance(item, dict):
            value = _url_from_package(item)
            if value:
                return value
    for item in params.get("files") or []:
        if isinstance(item, dict):
            value = _url_from_package(item)
            if value:
                return value
    return None


def collect_skill_packages(params: dict[str, Any]) -> list[dict[str, Any]]:
    """收集 directImport 需解压的 skill 压缩包（skill_packages 及 files 中的 zip/.skill）。"""
    packages: list[dict[str, Any]] = []
    seen: set[str] = set()

    def _add(item: dict[str, Any]) -> None:
        name = str(item.get("filename") or item.get("name") or "").strip()
        suffix = Path(name).suffix.lower()
        if suffix not in (".zip", ".skill"):
            return
        key = name or str(item.get("url") or item.get("base64Data") or id(item))
        if key in seen:
            return
        seen.add(key)
        packages.append(item)

    for item in params.get("skill_packages") or params.get("skillPackages") or []:
        if isinstance(item, dict):
            _add(item)
    for item in params.get("files") or []:
        if isinstance(item, dict):
            _add(item)
    return packages


def find_skill_root(skill_dir: Path) -> Path | None:
    """在 skill/ 下定位包含 SKILL.md 的技能根目录。"""
    if not skill_dir.is_dir():
        return None
    if (skill_dir / "SKILL.md").is_file():
        return skill_dir

    subdirs = [
        child
        for child in skill_dir.iterdir()
        if child.is_dir() and (child / "SKILL.md").is_file()
    ]
    if len(subdirs) == 1:
        return subdirs[0]
    if len(subdirs) > 1:
        names = ", ".join(sorted(d.name for d in subdirs))
        logger.warning("[directImport] multiple skill roots under %s: %s", skill_dir, names)
        return None

    for skill_md in skill_dir.rglob("SKILL.md"):
        return skill_md.parent
    return None


def _parse_skill_md(skill_md: Path) -> tuple[dict[str, str], str]:
    content = skill_md.read_text(encoding="utf-8")
    if not content.startswith("---"):
        raise ValueError("SKILL.md 缺少 YAML frontmatter（应以 --- 开头）")
    match = re.match(r"^---\n(.*?)\n---\n?(.*)", content, re.DOTALL)
    if not match:
        raise ValueError("SKILL.md frontmatter 格式无效")
    frontmatter = parse_frontmatter(match.group(1))
    body = match.group(2)
    return frontmatter, body


def validate_direct_import_skill(skill_root: Path) -> tuple[bool, str]:
    """按 directImport 规范校验 SKILL.md，收集全部不满足项后统一返回。"""
    errors: list[str] = []
    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        return False, "解压后未找到 SKILL.md"

    try:
        frontmatter, body = _parse_skill_md(skill_md)
    except ValueError as exc:
        return False, str(exc)

    name = str(frontmatter.get("name") or "").strip()
    if not name:
        errors.append("frontmatter 缺少必填字段 name（skill-name）")
    else:
        if not SKILL_NAME_PATTERN.match(name):
            errors.append(
                f"skill-name '{name}' 不符合规范：仅允许小写字母、数字、连字符 [a-z0-9-]，长度 1-64"
            )
        if name.startswith("-") or name.endswith("-") or "--" in name:
            errors.append(
                f"skill-name '{name}' 不能以 '-' 开头/结尾或包含连续 '--'"
            )
        if name != skill_root.name:
            errors.append(
                f"skill-name '{name}' 必须与父目录名 '{skill_root.name}' 一致"
            )

    description = normalize_skill_description(str(frontmatter.get("description") or ""))
    if not description:
        errors.append("description 不能为空")
    else:
        weighted_len = description_weighted_len(description)
        if weighted_len > DESCRIPTION_MAX_WEIGHTED:
            errors.append(
                f"description 加权字符数超限（{weighted_len} > {DESCRIPTION_MAX_WEIGHTED}；"
                f"中文计2、英文计1；纯中文≤{DESCRIPTION_MAX_CHARS_CJK}，"
                f"纯英文≤{DESCRIPTION_MAX_CHARS_EN}）"
            )
        desc_tokens = estimate_skill_tokens(description)
        if desc_tokens > DESCRIPTION_MAX_TOKENS:
            errors.append(
                f"description token 数超限（约 {desc_tokens} > {DESCRIPTION_MAX_TOKENS}；"
                f"中文 0.6 / 英文 0.3 token/字符）"
            )

    body_lines = body.splitlines()
    if not body.strip():
        errors.append("SKILL.md 正文不能为空")
    else:
        if len(body_lines) > BODY_MAX_LINES:
            errors.append(f"正文行数超限（{len(body_lines)} > {BODY_MAX_LINES}）")
        body_tokens = estimate_skill_tokens(body)
        if body_tokens > BODY_MAX_TOKENS:
            errors.append(
                f"正文 token 数超限（约 {body_tokens} > {BODY_MAX_TOKENS}；"
                f"中文 0.6 / 英文 0.3 token/字符）"
            )

    if errors:
        return False, "\n".join(f"- {item}" for item in errors)
    return True, "SKILL.md 校验通过"


def build_direct_import_fix_query(user_query: str, validation_message: str) -> str:
    """校验未通过时，引导 Agent 做最小改动修复并通过 skill-verifier 闸门。"""
    return (
        "请对 skill/ 下的已上传 skill 做**最小改动**规范化修改（保持原 skill 核心语义不变），"
        "然后通过 skill-verifier 闸门脚本完成校验与打包。不要询问用户。\n\n"
        "## directImport 校验未通过\n"
        f"{validation_message}\n\n"
        "## 修改原则\n"
        "- 最小改动：只做满足规范所必需的修改，不改变原 skill 用途与行为。\n"
        "- name 须为合法 kebab-case 且与目录名一致。\n"
        "- description 超长则压缩（纯中文 ≤512，纯英文 ≤1024，中英混杂时中文计2/英文计1且加权总和 ≤1024，且 ≤300 token）；不含尖括号。\n"
        "- 正文 ≤500 行且 ≤5000 token；超长则拆到 references/ 并用相对路径引用。\n\n"
        "完成修改后运行完整闸门：\n"
        '- cd "<skill-verifier-dir>" && python3 -m scripts.gate <workspace>'
    )


async def extract_packages_to_skill_dir(
    skill_dir: Path,
    packages: list[dict[str, Any]],
) -> None:
    """将上传的 zip/.skill 解压到 skill/ 并删除压缩包文件。"""
    skill_dir.mkdir(parents=True, exist_ok=True)
    if not packages:
        raise ValueError("directImport 缺少 skill 压缩包（skill_packages 或 zip 类型 files）")

    for index, res in enumerate(packages):
        name = str(res.get("filename") or res.get("name") or f"imported-{index}.zip").strip()
        suffix = Path(name).suffix.lower()
        if suffix not in (".zip", ".skill"):
            raise ValueError(f"不支持的 skill 包格式: {name}")

        download_url = str(res.get("url") or "").strip()
        content_b64 = str(res.get("base64Data") or res.get("base64") or "").strip()
        archive_path = skill_dir / f"_direct_import{suffix}"

        if download_url:
            await download_file(download_url, str(archive_path))
        elif content_b64:
            archive_path.write_bytes(base64.b64decode(content_b64))
        else:
            raise ValueError(f"skill 包 [{name}] 缺少 url 或 base64Data")

        safe_extract_zip(archive_path, skill_dir, extract_to_stem_dir=False)
        archive_path.unlink(missing_ok=True)


def _should_exclude(rel_path: Path) -> bool:
    parts = rel_path.parts
    if any(part in EXCLUDE_DIRS for part in parts):
        return True
    if len(parts) > 1 and parts[1] in ROOT_EXCLUDE_DIRS:
        return True
    name = rel_path.name
    if name in EXCLUDE_FILES:
        return True
    return any(fnmatch.fnmatch(name, pat) for pat in EXCLUDE_GLOBS)


def _workspace_for_skill(skill_path: Path) -> Path:
    if skill_path.parent.name == "skill":
        return skill_path.parent.parent
    return skill_path.parent


def _copy_dependency_references(skill_path: Path) -> bool:
    """复制 SKILL.md metadata 声明的外部依赖到 skill 内（与 package_skill 对齐）。"""
    content = (skill_path / "SKILL.md").read_text(encoding="utf-8")
    match = re.match(r"^---\n(.*?)\n---", content, re.DOTALL)
    if not match:
        return True
    frontmatter = yaml.safe_load(match.group(1))
    if not isinstance(frontmatter, dict):
        return True
    metadata = frontmatter.get("metadata") or {}
    if not isinstance(metadata, dict) or not metadata:
        return True

    workspace_path = _workspace_for_skill(skill_path)
    reference_path = Path("references")
    source_pairs: list[tuple[Path, Path]] = []

    tools = metadata.get("tools") or []
    if isinstance(tools, list):
        for tool in tools:
            if not isinstance(tool, dict):
                continue
            plugin_id = str(tool.get("pluginId") or tool.get("plugin_id") or "").strip()
            tool_name = str(tool.get("toolName") or tool.get("tool_name") or "").strip()
            if not plugin_id or not tool_name:
                continue
            filename = f"{plugin_id}__{tool_name}.json"
            source_pairs.append((
                workspace_path / "resources" / "available-tools" / filename,
                reference_path / "tools" / filename,
            ))

    for meta_key, rel_parts in (
        (("agents", "agent_tools", "agentTools"), ("agents", "available_agents.json")),
        (("clis", "cli_tools", "cliTools"), ("clis", "available_clis.json")),
    ):
        if any(metadata.get(k) for k in meta_key):
            source_pairs.append((
                workspace_path / "resources" / rel_parts[0] / rel_parts[1],
                reference_path / rel_parts[0] / rel_parts[1],
            ))

    missing = [src for src, _ in source_pairs if not src.exists()]
    if missing:
        for src in missing:
            logger.warning("[directImport] dependency reference missing: %s", src)
        return False

    for source, rel_dest in source_pairs:
        dest = skill_path / rel_dest
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, dest)
    return True


def package_validated_skill(skill_root: Path, output_dir: Path) -> Path | None:
    """将已通过 directImport 校验的 skill 目录打包为 zip（不再跑 quick_validate）。"""
    skill_root = skill_root.resolve()
    skill_md = skill_root / "SKILL.md"
    if not skill_md.is_file():
        logger.error("[directImport] package failed: SKILL.md missing in %s", skill_root)
        return None
    #
    # if not _copy_dependency_references(skill_root):
    #     logger.error("[directImport] package failed: dependency references missing")
    #     return None

    output_dir.mkdir(parents=True, exist_ok=True)
    skill_name = skill_root.name
    skill_filename = output_dir / f"{skill_name}.zip"

    files_to_package: list[tuple[Path, Path]] = []
    for file_path in skill_root.rglob("*"):
        if not file_path.is_file():
            continue
        arcname = Path(skill_name) / file_path.relative_to(skill_root)
        if _should_exclude(arcname):
            continue
        files_to_package.append((file_path, arcname))

    try:
        with zipfile.ZipFile(skill_filename, "w", zipfile.ZIP_DEFLATED) as zipf:
            for file_path, arcname in files_to_package:
                zipf.write(file_path, arcname)
        logger.info("[directImport] packaged skill to %s", skill_filename)
        return skill_filename
    except Exception as exc:
        logger.exception("[directImport] package failed: %s", exc)
        return None
