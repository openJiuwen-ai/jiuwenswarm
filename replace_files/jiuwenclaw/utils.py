# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Path management for JiuWenClaw.

根目录见 ``USER_WORKSPACE_DIR``（默认 ``~/.jiuwenclaw``；可由环境变量 ``JIUWENCLAW_DATA_DIR`` 指定绝对路径）。

Runtime layout:
- <root>/config/config.yaml
- <root>/config/.env
- <root>/agent/home
- <root>/agent/jiuwenclaw_workspace（DeepAgent 标准工作空间）
  - memory/
  - skills/
  - todo/
  - messages/
  - agents/
  - AGENT.md
  - IDENTITY.md
  - SOUL.md
  - HEARTBEAT.md
  - USER.md
- <root>/agent/sessions
- <root>/agent/jiuwenclaw_workspace/agent-data.json
- <root>/agent/.checkpoint
- <root>/agent/.logs（gateway.log / channel.log / agent_server.log）

内置模板位于包内 ``jiuwenclaw/resources/``（含 ``agent/`` 下各技能模板以及 ``skills_state.json``）。
"""

import json
import os
import re
import sys
import time
import asyncio
import shutil
import mimetypes
from pathlib import Path
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Literal, Optional
from collections import OrderedDict
import logging
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap


@dataclass
class FileTransferStartParams:
    """文件传输开始参数（用于封装多参数方法调用）."""

    transfer_id: str
    filename: str
    file_size: int
    sha256: str
    total_chunks: int
    chunk_size: int
    mime_type: str = ""
    session_id: str = ""
    channel_id: str = ""
    user_id: str = ""


# 用户数据根（config、agent、.logs 等）。供 config 模块在 import 时读取；仅依赖 os/path，不引用本包其它模块。
_raw_data_dir = os.environ.get("JIUWENCLAW_DATA_DIR", "").strip()
USER_WORKSPACE_DIR = (
    Path(_raw_data_dir).expanduser().resolve() if _raw_data_dir else Path.home() / ".jiuwenclaw"
)

_user_home: Path | None = None


def get_user_home() -> Path:
    """Get the current user home directory."""
    global _user_home
    if _user_home is None:
        _user_home = Path.home()
    return _user_home


def set_user_home(path: Path, initialized: bool = False) -> None:
    """Set a custom user home directory.

    After calling this function, all path getters will return paths based on the new home directory.
    
    Args:
        path: The new user home directory path.
        initialized: If True, skip cache reset (use when paths are already initialized elsewhere).
    """
    global _user_home, _initialized, _config_dir, _workspace_dir, _root_dir
    _user_home = Path(path)
    if initialized:
        return
    _initialized = False
    _config_dir = None
    _workspace_dir = None
    _root_dir = None


def get_user_workspace_dir() -> Path:
    """用户数据根目录：若设置 ``JIUWENCLAW_DATA_DIR`` 则为其解析路径，否则为 ``<user home>/.jiuwenclaw``。"""
    if _raw_data_dir:
        return USER_WORKSPACE_DIR
    return get_user_home() / ".jiuwenclaw"


# Cache for resolved paths
_config_dir: Path | None = None
_workspace_dir: Path | None = None
_root_dir: Path | None = None
_is_package: bool | None = None
_initialized: bool = False


def _detect_installation_mode() -> bool:
    """Detect if running from a package installation (whl) or PyInstaller bundle."""
    global _is_package
    if _is_package is not None:
        return _is_package

    # PyInstaller 打包后使用用户工作区路径
    if getattr(sys, "frozen", False):
        _is_package = True
        return True

    # Check if module is in site-packages
    module_file = Path(__file__).resolve()

    # Check if module file is in any site-packages directory
    for path in sys.path:
        site_packages = Path(path)
        if "site-packages" in str(site_packages) and site_packages in module_file.parents:
            _is_package = True
            return True

    _is_package = False
    return False


def _find_source_root() -> Path:
    """Find the repository root in development mode (contains jiuwenclaw/ package)."""
    current = Path(__file__).resolve().parent.parent
    jw_pkg = current / "jiuwenclaw"
    if (jw_pkg / "resources" / "agent").exists():
        return current
    parent = current.parent
    jw_pkg2 = parent / "jiuwenclaw"
    if (jw_pkg2 / "resources" / "agent").exists():
        return parent
    return current


def _find_package_root() -> Path | None:
    """Best-effort detection of the jiuwenclaw package root.

    In package mode (whl), __file__ is at site-packages/jiuwenclaw/paths.py,
    so parent is site-packages/jiuwenclaw/.
    In editable / source mode, __file__ is at <project>/jiuwenclaw/paths.py,
    so parent is <project>/jiuwenclaw/.
    """
    current = Path(__file__).resolve().parent
    return current


def _resolve_preferred_language(
        config_yaml_dest: Path, explicit: Optional[str]
) -> str:
    """确定初始化使用的语言：显式参数优先，否则读已复制的 config，默认 zh。"""
    if explicit is not None:
        lang = str(explicit).strip().lower()
        return lang if lang in ("zh", "en") else "zh"
    if config_yaml_dest.exists():
        try:
            rt = YAML()
            with open(config_yaml_dest, "r", encoding="utf-8") as f:
                data = rt.load(f) or {}
            lang = str(data.get("preferred_language") or "zh").strip().lower()
            if lang in ("zh", "en"):
                return lang
        except Exception as e:
            logger.error(f"Failed to load config.yaml: {e}")
    return "zh"


def prompt_preferred_language() -> Optional[Literal["zh", "en"]]:
    """交互询问语言偏好。仅接受明确选项；空输入、不在列表或取消用语 → 返回 None（调用方应终止 init）。"""
    print()
    print("[jiuwenclaw-init] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("[jiuwenclaw-init]  请选择默认语言 / Choose your default language")
    print("[jiuwenclaw-init] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("[jiuwenclaw-init]   [1] 中文（简体）")
    print("[jiuwenclaw-init]       → config: preferred_language: zh")
    print("[jiuwenclaw-init]   ────────────────────────────────────────────")
    print("[jiuwenclaw-init]   [2] English")
    print("[jiuwenclaw-init]       → config: preferred_language: en")
    print("[jiuwenclaw-init] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    print("[jiuwenclaw-init]  须明确选择：1 / 2 / zh / en（无默认语言）")
    print("[jiuwenclaw-init]  取消：no / n / q / cancel / 取消")
    print("[jiuwenclaw-init] ━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━")
    raw = input(
        "[jiuwenclaw-init] 请输入选项 (1, 2, zh, en) 或 no 取消: "
    ).strip().lower()
    if raw in ("no", "n", "q", "quit", "cancel", "取消"):
        return None
    if raw in ("1", "zh", "中文", "chinese"):
        return "zh"
    if raw in ("2", "en", "english", "e", "英文"):
        return "en"
    print("[jiuwenclaw-init] 无效选项；未选择有效语言，初始化已取消（与拒绝 yes/no 相同）。")
    return None


def _get_builtin_skill_names() -> set[str]:
    """Get the set of built-in skill names from package resources."""
    builtin_skills_dir = get_builtin_skills_dir()
    if not builtin_skills_dir.exists():
        return set()
    return {item.name for item in builtin_skills_dir.iterdir() if item.is_dir()}


def _migrate_legacy_workspace(
        workspace_dir: Path,
        preferred_language: Optional[str] = None,
) -> None:
    """Migrate from legacy layout to new DeepAgent workspace layout.

    Migration:
    - Old: ~/.jiuwenclaw/agent/workspace/ (agent-data.json here)
    - Old: ~/.jiuwenclaw/agent/home/ (PRINCIPLE.md, TONE.md, HEARTBEAT.md)
    - Old: ~/.jiuwenclaw/agent/skills/
    - Old: ~/.jiuwenclaw/agent/memory/

    - New: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/ (DeepAgent standard)

    Mapping:
    - agent/workspace/ -> agent/jiuwenclaw_workspace/ (main workspace)
    - agent/home/HEARTBEAT.md -> agent/jiuwenclaw_workspace/HEARTBEAT.md
    - agent/skills/ -> agent/jiuwenclaw_workspace/skills/
    - agent/memory/ -> agent/jiuwenclaw_workspace/memory/

    Args:
        workspace_dir: Path to workspace root (~/.jiuwenclaw).
        preferred_language: Preferred language for config (zh/en).
    """
    logger.info(f"Migrating from legacy layout: {workspace_dir}")

    old_workspace = workspace_dir / "agent" / "workspace"
    old_home = workspace_dir / "agent" / "home"
    old_skills = workspace_dir / "agent" / "skills"
    old_memory = workspace_dir / "agent" / "memory"

    new_workspace = workspace_dir / "agent" / "jiuwenclaw_workspace"
    new_workspace.mkdir(parents=True, exist_ok=True)

    # 1. Migrate old workspace contents
    if old_workspace.exists():
        for item in old_workspace.iterdir():
            dest = new_workspace / item.name
            if item.is_dir():
                if dest.exists():
                    shutil.rmtree(dest)
                shutil.copytree(item, dest)
            else:
                shutil.copy2(item, dest)
        logger.info(f"Migrated workspace: {old_workspace} -> {new_workspace}")

    # 2. Migrate old home files
    if old_home.exists():
        # HEARTBEAT.md -> HEARTBEAT.md (if not exists in new location)
        old_heartbeat = old_home / "HEARTBEAT.md"
        new_heartbeat = new_workspace / "HEARTBEAT.md"
        if old_heartbeat.exists() and not new_heartbeat.exists():
            shutil.copy2(old_heartbeat, new_heartbeat)
            logger.info("Migrated HEARTBEAT.md from home")

        # Merge PRINCIPLE.md and TONE.md into SOUL.md
        old_principle = old_home / "PRINCIPLE.md"
        old_tone = old_home / "TONE.md"
        new_soul = new_workspace / "SOUL.md"
        if not new_soul.exists() and (old_principle.exists() or old_tone.exists()):
            soul_content = ["# Agent Soul\n\n"]
            if old_principle.exists():
                principle_text = old_principle.read_text(encoding="utf-8")
                soul_content.append("## Principles\n\n")
                soul_content.append(principle_text)
                soul_content.append("\n\n")
            if old_tone.exists():
                tone_text = old_tone.read_text(encoding="utf-8")
                soul_content.append("## Tone\n\n")
                soul_content.append(tone_text)
                soul_content.append("\n\n")
            new_soul.write_text("".join(soul_content), encoding="utf-8")
            logger.info("Merged PRINCIPLE.md and TONE.md into SOUL.md")

    new_skills = new_workspace / "skills"
    if old_skills.exists():
        if new_skills.exists():
            shutil.rmtree(new_skills)
        shutil.copytree(old_skills, new_skills)
        logger.info(f"Migrated skills: {old_skills} -> {new_skills}")

        builtin_skill_names = _get_builtin_skill_names()
        for skill_dir in new_skills.iterdir():
            if skill_dir.is_dir() and (skill_dir.name in builtin_skill_names \
                                       or skill_dir.name in ["daily-report", "skill-creation"]):
                shutil.rmtree(skill_dir)

    # 4. Migrate memory
    new_memory = new_workspace / "memory"
    new_memory.mkdir(parents=True, exist_ok=True)

    if old_memory.exists():
        # 4.1 Migrate USER.md to workspace root (not in memory/)
        old_user = old_memory / "USER.md"
        new_user = new_workspace / "USER.md"
        if old_user.exists() and not new_user.exists():
            shutil.copy2(old_user, new_user)
            logger.info("Migrated USER.md from memory/ to workspace root")

        # 4.2 Create daily_memory directory
        daily_memory = new_memory / "daily_memory"
        daily_memory.mkdir(parents=True, exist_ok=True)

        # 4.3 Merge memory files (skip if already exists)
        # Date pattern: YYYY-MM-DD.md (e.g., 2026-04-14.md)
        date_pattern = re.compile(r"^\d{4}-\d{2}-\d{2}\.md$")

        for item in old_memory.iterdir():
            if item.name == "USER.md":
                continue  # Already handled above
            if item.name == "MEMORY.md":
                dest = new_memory / "MEMORY.md"
                if not dest.exists():
                    shutil.copy2(item, dest)
                    logger.info("Migrated MEMORY.md")
            elif item.is_file():
                # Date-based memory files (YYYY-MM-DD.md) -> daily_memory/
                # Other files -> new_memory/ root
                dest = daily_memory / item.name if date_pattern.match(item.name) else new_memory / item.name
                if not dest.exists():
                    shutil.copy2(item, dest)
                    logger.info(f"Migrated memory file: {item.name}")
            elif item.is_dir():
                # Other directories (e.g., specific memory categories)
                dest = new_memory / item.name
                if not dest.exists():
                    shutil.copytree(item, dest)
                    logger.info(f"Migrated memory directory: {item.name}")

        logger.info(f"Migrated memory: {old_memory} -> {new_memory}")

    # 5. Migrate cron_jobs.json from old_home to gateway
    # This ensures cron jobs are not lost during migration
    old_cron_jobs = old_home / "cron_jobs.json"
    gateway_dir = workspace_dir / "gateway"
    new_cron_jobs = gateway_dir / "cron_jobs.json"
    if old_cron_jobs.exists():
        gateway_dir.mkdir(parents=True, exist_ok=True)
        try:
            # Read old cron jobs data
            old_data = json.loads(old_cron_jobs.read_text(encoding="utf-8"))
            # Add 'expired': false to each job if not present (schema migration)
            if "jobs" in old_data and isinstance(old_data["jobs"], list):
                for job in old_data["jobs"]:
                    if isinstance(job, dict) and "expired" not in job:
                        job["expired"] = False
            if not new_cron_jobs.exists():
                # Write migrated data to new location
                new_cron_jobs.write_text(
                    json.dumps(old_data, ensure_ascii=False, indent=2),
                    encoding="utf-8"
                )
                logger.info(f"Migrated cron_jobs.json: {old_cron_jobs} -> {new_cron_jobs}")
            else:
                # Both exist - backup old, log warning
                backup_cron = gateway_dir / f"cron_jobs.json.backup.{int(time.time())}"
                shutil.copy2(old_cron_jobs, backup_cron)
                logger.warning(
                    f"Both old and new cron_jobs.json exist. "
                    f"Kept new version, backed up old to {backup_cron}"
                )
        except (json.JSONDecodeError, IOError) as e:
            logger.error(f"Failed to migrate cron_jobs.json: {e}")

    # 6. Clean up old directories after successful migration
    try:
        if old_workspace.exists():
            shutil.rmtree(old_workspace)
            logger.info(f"Removed old workspace: {old_workspace}")
        if old_home.exists():
            shutil.rmtree(old_home)
            logger.info(f"Removed old home: {old_home}")
        if old_skills.exists():
            shutil.rmtree(old_skills)
            logger.info(f"Removed old skills: {old_skills}")
        if old_memory.exists():
            shutil.rmtree(old_memory)
            logger.info(f"Removed old memory: {old_memory}")
    except OSError as e:
        logger.warning(f"Failed to remove some old directories: {e}")

    logger.info(f"Migration completed: {new_workspace}")


def cleanup_team_files(workspace_dir: Path) -> None:
    """清理 Team 旧版本遗留的文件和目录.

    Legacy cleanup:
    - Old: {workspace_dir}/workspace/ (旧版本 team workspace)
    - Old: {workspace_dir}/agent/team_data/ (旧版本 team 数据库目录)
    - Old: {workspace_dir}/team.db (旧版本 team 数据库文件)
    - Old: {workspace_dir}/team.db-wal (旧版本 team WAL 文件)
    - Old: {workspace_dir}/team.db-shm (旧版本 team SHM 文件)
    - Old: {workspace_dir}/agent/team.db (旧版本 team 数据库文件)
    - Old: {workspace_dir}/agent/team.db-wal (旧版本 team WAL 文件)
    - Old: {workspace_dir}/agent/team.db-shm (旧版本 team SHM 文件)

    Args:
        workspace_dir: JiuWenClaw 用户工作空间根目录 (~/.jiuwenclaw)
    """
    agent_dir = workspace_dir / "agent"

    # 清理 {workspace_dir}/workspace/ (旧版本 team workspace)
    legacy_workspace = workspace_dir / "workspace"
    if legacy_workspace.exists():
        try:
            shutil.rmtree(legacy_workspace)
            logger.info(f"[Cleanup] Removed legacy workspace directory: {legacy_workspace}")
        except OSError as e:
            logger.warning(f"[Cleanup] Failed to remove legacy workspace directory: {e}")

    # 清理 {workspace_dir}/agent/team_data/ (旧版本 team 数据库目录)
    legacy_team_data = agent_dir / "team_data"
    if legacy_team_data.exists():
        try:
            shutil.rmtree(legacy_team_data)
            logger.info(f"[Cleanup] Removed legacy team_data directory: {legacy_team_data}")
        except OSError as e:
            logger.warning(f"[Cleanup] Failed to remove legacy team_data directory: {e}")

    # 清理 {workspace_dir}/team.db* (旧版本 team 数据库文件)
    legacy_team_db_root = workspace_dir / "team.db"
    for suffix in ["", "-wal", "-shm"]:
        db_file = legacy_team_db_root.with_suffix(".db" + suffix)
        if db_file.exists():
            try:
                db_file.unlink()
                logger.info(f"[Cleanup] Removed legacy team database file: {db_file}")
            except OSError as e:
                logger.warning(f"[Cleanup] Failed to remove legacy team database file: {e}")

    # 清理 {workspace_dir}/agent/team.db* (旧版本 team 数据库文件)
    legacy_team_db_agent = agent_dir / "team.db"
    for suffix in ["", "-wal", "-shm"]:
        db_file = legacy_team_db_agent.with_suffix(".db" + suffix)
        if db_file.exists():
            try:
                db_file.unlink()
                logger.info(f"[Cleanup] Removed legacy team database file: {db_file}")
            except OSError as e:
                logger.warning(f"[Cleanup] Failed to remove legacy team database file: {e}")


def _deep_merge_from_source(source, user):
    """将 source（源码模板）中的新增字段合并到 user（用户配置）。

    规则（以源码模板的 key 顺序为准，确保新增字段插入在正确位置）：
    - source 有、user 没有 → 新增到源码中该 key 所在的位置
    - 两边都有且都是 dict/CommentedMap → 递归合并
    - 两边都有但类型不同或 user 有值 → 保留 user 的值

    返回值类型与 source 一致（保持 ruamel CommentedMap 的顺序）。
    """
    result = CommentedMap()
    src_keys = list(source.keys())
    user_keys = [k for k in user.keys() if k not in src_keys]  # 用户独有但源码已删的 key

    for key in src_keys:
        src_val = source[key]
        if key not in user:
            # 新增字段：按源码位置插入
            result[key] = src_val
        elif isinstance(src_val, dict) and isinstance(user[key], dict):
            # 两边都是 dict → 递归
            merged_sub = _deep_merge_from_source(src_val, user[key])
            result[key] = merged_sub
        else:
            # 保留用户值
            result[key] = user[key]

    # 用户独有 key（源码没有的），追加到末尾
    for key in user_keys:
        result[key] = user[key]

    return result


def _merge_config_from_source(src: Path, dest: Path) -> None:
    """将源码 config.yaml 的新增字段同步到用户 config.yaml（不覆盖用户值）。"""
    try:
        rt = YAML()
        rt.preserve_quotes = True
        rt.default_flow_style = False
        rt.indent(mapping=2, sequence=4, offset=2)
        rt.width = 4096

        with open(src, "r", encoding="utf-8") as f:
            src_data = rt.load(f)
        with open(dest, "r", encoding="utf-8") as f:
            user_data = rt.load(f)

        if src_data is None or user_data is None:
            return

        merged = _deep_merge_from_source(src_data, user_data)

        with open(dest, "w", encoding="utf-8") as f:
            rt.dump(merged, f)
    except Exception as e:
        # 合并失败不影响正常启动，记录日志即可
        logging.getLogger(__name__).warning(
            "Failed to merge config from source %s -> %s: %s", src, dest, e
        )


def update_config():
    package_root = _find_package_root()
    if not package_root:
        raise RuntimeError("package root not found")

    workspace_dir = get_user_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # ----- config: copy config.yaml -----
    resources_dir = package_root / "resources"
    config_yaml_src_candidates = [
        resources_dir / "config.yaml",
        package_root / "config" / "config.yaml",
    ]

    config_yaml_src = next((p for p in config_yaml_src_candidates if p.exists()), None)

    if not config_yaml_src:
        raise RuntimeError(
            "config.yaml template not found; tried: "
            + ", ".join(str(p) for p in config_yaml_src_candidates)
        )

    config_dest_dir = workspace_dir / "config"
    config_dest_dir.mkdir(parents=True, exist_ok=True)
    config_yaml_dest = config_dest_dir / "config.yaml"
    # 将源码 config.yaml 的新增字段合并到用户 config.yaml，保留用户已有值
    _merge_config_from_source(config_yaml_src, config_yaml_dest)


def prepare_workspace(
        overwrite: bool = True,
        preferred_language: Optional[str] = None,
) -> None:
    package_root = _find_package_root()
    if not package_root:
        raise RuntimeError("package root not found")

    workspace_dir = get_user_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # Check for legacy workspace migration or cleanup
    old_workspace = workspace_dir / "agent" / "workspace"
    old_home = workspace_dir / "agent" / "home"
    old_skills = workspace_dir / "agent" / "skills"
    old_memory = workspace_dir / "agent" / "memory"

    # Check for legacy directory migration (for start command, overwrite=False)
    # Migration triggers when ANY legacy directory exists, not just old_workspace
    legacy_dirs_exist = (
            old_workspace.exists() or old_skills.exists() or old_memory.exists()
    )

    if legacy_dirs_exist and not overwrite:
        _migrate_legacy_workspace(workspace_dir, preferred_language)
    # If overwrite (init command), clean up old legacy directories first
    elif overwrite:
        try:
            if old_workspace.exists():
                shutil.rmtree(old_workspace)
                logger.info(f"Removed old workspace: {old_workspace}")
            if old_home.exists():
                shutil.rmtree(old_home)
                logger.info(f"Removed old home: {old_home}")
            if old_skills.exists():
                shutil.rmtree(old_skills)
                logger.info(f"Removed old skills: {old_skills}")
            if old_memory.exists():
                shutil.rmtree(old_memory)
                logger.info(f"Removed old memory: {old_memory}")
        except OSError as e:
            logger.warning(f"Failed to remove some old directories: {e}")

    # ----- config: copy config.yaml -----
    resources_dir = package_root / "resources"
    config_yaml_src_candidates = [
        resources_dir / "config.yaml",
        package_root / "config" / "config.yaml",
    ]

    config_yaml_src = next((p for p in config_yaml_src_candidates if p.exists()), None)

    if not config_yaml_src:
        raise RuntimeError(
            "config.yaml template not found; tried: "
            + ", ".join(str(p) for p in config_yaml_src_candidates)
        )

    config_dest_dir = workspace_dir / "config"
    config_dest_dir.mkdir(parents=True, exist_ok=True)
    config_yaml_dest = config_dest_dir / "config.yaml"

    if overwrite or not config_yaml_dest.exists():
        shutil.copy2(config_yaml_src, config_yaml_dest)

    builtin_rules_src = resources_dir / "builtin_rules.yaml"
    builtin_rules_dest = config_dest_dir / "builtin_rules.yaml"
    if builtin_rules_src.is_file() and (overwrite or not builtin_rules_dest.exists()):
        shutil.copy2(builtin_rules_src, builtin_rules_dest)

    resolved_lang = _resolve_preferred_language(config_yaml_dest, preferred_language)

    # ----- 内置模板根目录：<package>/resources（含 agent/、skills_state.json）-----
    template_root = resources_dir
    template_agent_dir = template_root / "agent"
    if not template_agent_dir.is_dir():
        raise RuntimeError(f"resources template missing agent dir: {template_agent_dir}")

    # ----- .env: copy from template to config/.env -----
    env_template_src_candidates = [
        resources_dir / ".env.template",
        package_root / ".env.template",
    ]
    env_template_src = next((p for p in env_template_src_candidates if p.exists()), None)
    if not env_template_src:
        raise RuntimeError(
            "env template source not found; tried: "
            + ", ".join(str(p) for p in env_template_src_candidates)
        )
    env_dest = workspace_dir / "config" / ".env"
    if overwrite or not env_dest.exists():
        shutil.copy2(env_template_src, env_dest)

    # ----- copy runtime dirs (new multi-tenant layout) -----
    service_root = get_service_root_dir()
    service_root.mkdir(parents=True, exist_ok=True)
    (service_root / ".logs").mkdir(parents=True, exist_ok=True)

    agent_workspace = get_multi_tenant_user_workspace_dir("default", "default")
    if agent_workspace:
        agent_workspace.mkdir(parents=True, exist_ok=True)
        (agent_workspace / ".checkpoint").mkdir(parents=True, exist_ok=True)
        agent_root = agent_workspace / "agent"
        agent_root.mkdir(parents=True, exist_ok=True)
    else:
        agent_root = workspace_dir / "agent"

    agent_sessions = agent_root / "sessions"

    # ----- DeepAgent workspace (standard DeepAgents schema) -----
    deepagent_workspace = agent_root / "jiuwenclaw_workspace"
    agent_skills = deepagent_workspace / "skills"
    agent_memory = deepagent_workspace / "memory"

    template_agent_workspace = template_agent_dir / "jiuwenclaw_workspace"
    template_agent_memory = template_agent_dir / "jiuwenclaw_workspace" / "memory"

    def _copy_dir(
            src_dir: Path,
            dst_dir: Path,
            ignore_patterns: tuple[str, ...] | None = None,
    ) -> None:
        if not src_dir.exists():
            return
        if overwrite and dst_dir.exists():
            shutil.rmtree(dst_dir)
        dst_dir.parent.mkdir(parents=True, exist_ok=True)

        if ignore_patterns:
            ignore = shutil.ignore_patterns(*ignore_patterns)
        else:
            ignore = None

        if not dst_dir.exists():
            shutil.copytree(src_dir, dst_dir, ignore=ignore)
        else:
            shutil.copytree(src_dir, dst_dir, dirs_exist_ok=True, ignore=ignore)

    # Copy DeepAgent workspace template (includes agent-data.json, memory, skills)
    # Ignore _ZH.md and _EN.md files - they are handled separately
    if template_agent_workspace.exists():
        _copy_dir(
            template_agent_workspace,
            deepagent_workspace,
            ignore_patterns=("*_ZH.md", "*_EN.md", "skills"),
        )
    else:
        deepagent_workspace.mkdir(parents=True, exist_ok=True)
    _copy_dir(template_agent_memory, agent_memory, ignore_patterns=("*_ZH.md", "*_EN.md"))

    # Copy multi-language files based on resolved language
    # Files with _ZH/_EN suffix are copied to the workspace without suffix
    suffix = "_ZH" if resolved_lang == "zh" else "_EN"
    multilang_files = [
        (f"AGENT{suffix}.md", "AGENT.md"),
        (f"HEARTBEAT{suffix}.md", "HEARTBEAT.md"),
        (f"IDENTITY{suffix}.md", "IDENTITY.md"),
        (f"SOUL{suffix}.md", "SOUL.md"),
        (f"memory/MEMORY{suffix}.md", "memory/MEMORY.md"),
    ]
    for src_name, dst_name in multilang_files:
        src_path = template_agent_workspace / src_name
        dst_path = deepagent_workspace / dst_name
        if src_path.exists() and not dst_path.exists():
            shutil.copy2(src_path, dst_path)

    # skills state: shipped under resources/
    skills_state_src = template_root / "skills_state.json"
    if skills_state_src.exists():
        agent_skills.mkdir(parents=True, exist_ok=True)
        dest_skill_state = agent_skills / "skills_state.json"
        if not dest_skill_state.exists():
            shutil.copy2(skills_state_src, agent_skills / "skills_state.json")

    # sessions is runtime-only (template may not include it)
    agent_sessions.mkdir(parents=True, exist_ok=True)

    from jiuwenclaw.config import migrate_config_from_template, set_preferred_language_in_config_file

    migrate_config_from_template(config_yaml_src, config_yaml_dest)
    set_preferred_language_in_config_file(config_yaml_dest, resolved_lang)
    # 由于日志初始化在前, 调用过_resolve_paths。workspace完成后务必要再调用一次_resolve_paths
    _resolve_paths(force=True)


def init_user_workspace(overwrite: bool = True) -> Path | Literal["cancelled"]:
    """Initialize ~/.jiuwenclaw from package or source resources.

    资源布局:
    - 模板配置:   <package_root>/resources/config.yaml
    - .env 模板: <package_root>/resources/.env.template
    - 数据模板:   <package_root>/resources/agent（含各技能模板）、skills_state.json

    上述内容会被复制到:
    - ~/.jiuwenclaw/config/config.yaml（含 preferred_language）
    - ~/.jiuwenclaw/config/builtin_rules.yaml（内置 shell 安全规则模板，与 config 同目录）
    - ~/.jiuwenclaw/config/.env
    - ~/.jiuwenclaw/agent/...

    注意：PRINCIPLE.md、TONE.md、HEARTBEAT.md 已被 SOUL.md 和新的心跳机制替代，
    不再由 JiuwenClaw 复制到用户工作区。

    交互式 init 会先询问语言；首次启动 app 时非交互 prepare_workspace 则沿用模板 config 中的语言。

    Args:
        overwrite: True 时强制清理整个工作空间目录后初始化；
                   False 时保留原有数据，执行迁移合并逻辑。
    """
    workspace_dir = get_user_workspace_dir()
    if workspace_dir.exists():
        if overwrite:
            # Force mode: explain both modes and ask for confirmation
            print(
                "[jiuwenclaw-init] With -f/--force flag, "
                "entire ~/.jiuwenclaw will be deleted for clean initialization."
            )
            print("[jiuwenclaw-init] WARNING: This will delete all historical configuration and memory information.")
            print("[jiuwenclaw-init] This action cannot be undone.")
            confirmation = input(
                "[jiuwenclaw-init] Do you want to confirm reinitialization? (yes/no): "
            ).strip().lower()

            if confirmation not in ("yes", "y"):
                print("[jiuwenclaw-init] Initialization cancelled. Exiting.")
                return "cancelled"

            # Delete entire workspace directory for clean initialization
            try:
                shutil.rmtree(workspace_dir)
                logger.info(f"Removed workspace directory: {workspace_dir}")
            except OSError as e:
                logger.error(f"Failed to remove workspace directory: {e}")
                print(f"[jiuwenclaw-init] ERROR: Failed to remove workspace: {e}")
                return "cancelled"
        else:
            # Merge mode: inform about preservation
            print(
                "[jiuwenclaw-init] Without -f/--force flag, "
                "existing files will be preserved and merged with template."
            )
            print("[jiuwenclaw-init] This action cannot be undone.")
            confirmation = input("[jiuwenclaw-init] Do you want to continue? (yes/no): ").strip().lower()

            if confirmation not in ("yes", "y"):
                print("[jiuwenclaw-init] Initialization cancelled. Exiting.")
                return "cancelled"

    lang = prompt_preferred_language()
    if lang is None:
        print("[jiuwenclaw-init] Initialization cancelled. Exiting.")
        return "cancelled"
    print(f"[jiuwenclaw-init] 将使用语言 / Language: {lang}")
    prepare_workspace(overwrite, preferred_language=lang)

    return workspace_dir


def _resolve_paths(force=False) -> None:
    """Resolve and cache all paths."""
    global _initialized, _config_dir, _workspace_dir, _root_dir

    if not force and _initialized:
        return

    workspace_dir = get_user_workspace_dir()
    # 优先使用已初始化的用户工作区 (~/.jiuwenclaw)，
    # 保证源码运行与安装包运行后的读写路径完全一致。
    user_config_dir = workspace_dir / "config"
    # 多租户路径：service_default/agent_default/agent/jiuwenclaw_workspace
    multi_tenant_workspace = get_multi_tenant_user_workspace_dir("default", "default")
    if multi_tenant_workspace:
        user_workspace_dir = multi_tenant_workspace / "agent" / "jiuwenclaw_workspace"
    else:
        user_workspace_dir = workspace_dir / "agent" / "jiuwenclaw_workspace"
    if user_config_dir.exists():
        _root_dir = workspace_dir
        _config_dir = user_config_dir
        _workspace_dir = user_workspace_dir
    else:
        # 尚未初始化 ~/.jiuwenclaw：从包内 resources 直读配置，工作区指向包内 agent/jiuwenclaw_workspace
        package_root = _find_package_root()
        if package_root and (package_root / "resources" / "config.yaml").exists():
            res = package_root / "resources"
            _root_dir = package_root.parent
            _config_dir = res
            _workspace_dir = res / "agent" / "jiuwenclaw_workspace"
            _workspace_dir.mkdir(parents=True, exist_ok=True)
        else:
            source_root = _find_source_root()
            pkg = source_root / "jiuwenclaw"
            res = pkg / "resources"
            _root_dir = source_root
            _config_dir = res if (res / "config.yaml").exists() else source_root / "config"
            _workspace_dir = res / "agent" / "jiuwenclaw_workspace"
            _workspace_dir.mkdir(parents=True, exist_ok=True)

    _initialized = True


def get_config_dir() -> Path:
    """Get the config directory path."""
    _resolve_paths()
    return _config_dir


def get_workspace_dir() -> Path:
    """Get the workspace directory path."""
    _resolve_paths()
    return _workspace_dir


def get_root_dir() -> Path:
    """Get the root directory path."""
    _resolve_paths()
    return _root_dir


def get_agent_workspace_dir() -> Path:
    """Get the agent workspace directory path.

    This is the DeepAgent standard workspace directory under the agent root.
    It contains standard nodes like skills, memory, todo, messages, etc.

    Returns:
        Path to agent workspace: ~/.jiuwenclaw/agent/jiuwenclaw_workspace
    """
    return get_agent_root_dir() / "jiuwenclaw_workspace"


def get_service_root_dir(service_id: str = "default") -> Path:
    """Get the service-level directory path.

    多租户架构下，service 级别存放共享数据（如日志）。
    Path: ~/.jiuwenclaw/service_{service_id}/

    Args:
        service_id: 服务 ID，默认为 "default"
    """
    return get_user_workspace_dir() / f"service_{service_id}"


def get_agent_root_dir() -> Path:
    """Get the agent root directory path (multi-tenant default).

    单租户作为多租户的默认特例，返回默认多租户路径。
    Path: ~/.jiuwenclaw/service_default/agent_default/agent/
    """
    return get_multi_tenant_user_workspace_dir("default", "default") / "agent"


def get_agent_root_relative_dir() -> Path:
    """Get the agent root relative path under user workspace."""
    return Path("agent")


def get_agent_workspace_relative_dir() -> Path:
    """Get the agent workspace relative path under user workspace."""
    return get_agent_root_relative_dir() / "jiuwenclaw_workspace"


def get_agent_sessions_relative_dir() -> Path:
    """Get the agent sessions relative path under user workspace."""
    return get_agent_root_relative_dir() / "sessions"


def get_multi_tenant_user_workspace_dir(service_id: str | None, agent_id: str | None) -> Path | None:
    """Get multi-tenant user workspace directory path.

    Path format: ~/.jiuwenclaw/service_{service_id}/agent_{agent_id}
    """
    if not service_id and not agent_id:
        return None
    workspace_dir = get_user_workspace_dir()
    workspace_dir = workspace_dir / f"service_{service_id}" if service_id else workspace_dir / "service"
    workspace_dir = workspace_dir / f"agent_{agent_id}" if agent_id else workspace_dir / "agents"
    return workspace_dir


def get_agent_home_dir() -> Path:
    return get_agent_root_dir() / "home"


def get_agent_memory_dir() -> Path:
    """Get the agent memory directory path.

    Uses DeepAgent standard workspace location for unified workspace.

    Returns:
        Path to memory directory: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/memory
    """
    return get_agent_workspace_dir() / "memory"


def get_agent_skills_dir() -> Path:
    """Get the agent skills directory path.

    Uses DeepAgent standard workspace location for unified workspace.

    Returns:
        Path to skills directory: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/skills
    """
    return get_agent_workspace_dir() / "skills"


def get_multi_tenant_skill_dirs(
        service_id: str | None, agent_id: str | None,
) -> list[Path]:
    """Resolve the skills directory list for multi-tenant / single-tenant mode.

    - Multi-tenant (any of ``service_id`` / ``agent_id`` provided): returns
      ``[<multi-tenant user workspace>/skills]``.
    - Single-tenant (both ``None``): returns ``[get_agent_skills_dir()]``.
    """
    if service_id or agent_id:
        workspace = get_multi_tenant_user_workspace_dir(service_id, agent_id)
        if workspace is not None:
            return [workspace / "skills"]
    return [get_agent_skills_dir()]


def get_shared_agent_skills_dirs() -> list[Path]:
    raw = (os.getenv("JIUWENCLAW_SHARED_SKILLS_DIRS") or "").strip()
    if not raw:
        return []

    dirs: list[Path] = []
    seen: set[str] = set()
    for part in [part.strip() for part in raw.split(os.pathsep) if part.strip()]:
        path = Path(part).expanduser().resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    return dirs


def get_agent_registered_skill_dirs() -> list[Path]:
    if get_shared_agent_skills_dirs():
        return get_shared_agent_skills_dirs()
    return [get_agent_skills_dir()]


def get_agent_tools_dir() -> Path:
    """落盘 MCP 工具配置目录（全局 ``~/.jiuwenclaw/agent/jiuwenclaw_workspace/tools``）。

    多租户 AgentServer 场景下由 ``ToolManager(get_tools_dir=...)`` 覆盖为当前租户工作区下的 ``tools``。
    """
    return get_agent_workspace_dir() / "tools"


def get_deepagent_todo_dir() -> Path:
    """Get the DeepAgent todo directory path.

    Returns:
        Path to todo directory: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/todo
    """
    return get_agent_workspace_dir() / "todo"


def get_deepagent_messages_dir() -> Path:
    """Get the DeepAgent messages directory path.

    Returns:
        Path to messages directory: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/messages
    """
    return get_agent_workspace_dir() / "messages"


def get_deepagent_agents_dir() -> Path:
    """Get the DeepAgent agents (sub-agent) directory path.

    Returns:
        Path to agents directory: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/agents
    """
    return get_agent_workspace_dir() / "agents"


def get_deepagent_heartbeat_path() -> Path:
    """Get the DeepAgent HEARTBEAT.md file path.

    Returns:
        Path to HEARTBEAT.md: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/HEARTBEAT.md
    """
    return get_agent_workspace_dir() / "HEARTBEAT.md"


def get_deepagent_agent_md_path() -> Path:
    """Get the DeepAgent AGENT.md file path.

    Returns:
        Path to AGENT.md: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/AGENT.md
    """
    return get_agent_workspace_dir() / "AGENT.md"


def get_deepagent_soul_md_path() -> Path:
    """Get the DeepAgent SOUL.md file path.

    Returns:
        Path to SOUL.md: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/SOUL.md
    """
    return get_agent_workspace_dir() / "SOUL.md"


def get_deepagent_identity_md_path() -> Path:
    """Get the DeepAgent IDENTITY.md file path.

    Returns:
        Path to IDENTITY.md: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/IDENTITY.md
    """
    return get_agent_workspace_dir() / "IDENTITY.md"


def get_deepagent_user_md_path() -> Path:
    """Get the DeepAgent USER.md file path.

    Returns:
        Path to USER.md: ~/.jiuwenclaw/agent/jiuwenclaw_workspace/USER.md
    """
    return get_agent_workspace_dir() / "USER.md"


def get_builtin_skills_dir() -> Path:
    """Get the built-in skills directory from package resources."""
    package_root = _find_package_root()
    # 优先检查 jiuwenclaw_workspace/skills 目录（标准布局）
    primary_path = package_root / "resources" / "agent" / "jiuwenclaw_workspace" / "skills"
    if primary_path.exists() and primary_path.is_dir():
        return primary_path
    # 回退到 skills 目录
    fallback_path = package_root / "resources" / "agent" / "skills"
    return fallback_path


def get_agent_sessions_dir() -> Path:
    """Get the default sessions directory path.

    返回默认多租户路径（单租户作为多租户的默认特例）：
    ~/.jiuwenclaw/service_default/agent_default/agent/sessions
    """
    return get_multi_tenant_user_workspace_dir("default", "default") / "agent" / "sessions"


_legacy_migration_done: bool = False


def _migrate_legacy_checkpoint_and_logs() -> None:
    """One-time migration: move legacy paths to multi-tenant structure.

    Migration paths:
    - ~/.jiuwenclaw/.checkpoint -> ~/.jiuwenclaw/service_default/agent_default/.checkpoint
    - ~/.jiuwenclaw/.logs -> ~/.jiuwenclaw/service_default/.logs
    - ~/.jiuwenclaw/agent/.checkpoint -> ~/.jiuwenclaw/service_default/agent_default/.checkpoint
    - ~/.jiuwenclaw/agent/.logs -> ~/.jiuwenclaw/service_default/.logs
    """
    global _legacy_migration_done
    if _legacy_migration_done:
        return
    _legacy_migration_done = True

    workspace = get_user_workspace_dir()

    # 目标路径
    service_root = get_service_root_dir()
    agent_workspace = get_multi_tenant_user_workspace_dir("default", "default")
    agent_root = get_agent_root_dir()

    # 确保 target 目录存在
    service_root.mkdir(parents=True, exist_ok=True)
    if agent_workspace:
        agent_workspace.mkdir(parents=True, exist_ok=True)
    agent_root.mkdir(parents=True, exist_ok=True)

    # 迁移 .checkpoint 到 agent_default 级别
    checkpoint_target = agent_workspace / ".checkpoint" if agent_workspace else agent_root / ".checkpoint"
    checkpoint_sources = [
        workspace / ".checkpoint",
        workspace / "agent" / ".checkpoint",
        agent_root / ".checkpoint",  # 从 agent 子目录迁移到 agent_default 级别
    ]
    for legacy in checkpoint_sources:
        if legacy.exists() and not checkpoint_target.exists():
            checkpoint_target.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(checkpoint_target))
        elif legacy.exists() and checkpoint_target.exists():
            # 合并 checkpoint 文件
            for f in legacy.iterdir():
                if not (checkpoint_target / f.name).exists():
                    shutil.move(str(f), str(checkpoint_target / f.name))

    # 迁移 .logs 到 service 级别
    logs_target = service_root / ".logs"
    logs_sources = [
        workspace / ".logs",
        workspace / "agent" / ".logs",
    ]
    for legacy in logs_sources:
        if legacy.exists() and not logs_target.exists():
            shutil.move(str(legacy), str(logs_target))
        elif legacy.exists() and logs_target.exists():
            # 合并日志：移动旧日志文件到新目录
            for f in legacy.iterdir():
                if not (logs_target / f.name).exists():
                    shutil.move(str(f), str(logs_target / f.name))


def get_checkpoint_dir(
        service_id: str | None = None,
        agent_id: str | None = None,
) -> Path:
    """Get the checkpoint directory path (agent_id level).

    多租户架构下，checkpoint 存放在 agent_id 级别。
    默认路径: ~/.jiuwenclaw/service_default/agent_default/.checkpoint
    租户路径: ~/.jiuwenclaw/service_{service_id}/agent_{agent_id}/.checkpoint
    """
    _migrate_legacy_checkpoint_and_logs()
    sid = service_id or "default"
    aid = agent_id or "default"
    workspace = get_multi_tenant_user_workspace_dir(sid, aid)
    if workspace:
        return workspace / ".checkpoint"
    return get_agent_root_dir() / ".checkpoint"


def get_logs_dir() -> Path:
    """Get the logs directory path (service-level).

    多租户架构下，日志存放在 service 级别，便于多 agent 共享。
    Path: ~/.jiuwenclaw/service_default/.logs
    """
    _migrate_legacy_checkpoint_and_logs()
    return get_service_root_dir() / ".logs"


def get_xy_tmp_dir() -> Path:
    workspace_dir = get_user_workspace_dir()
    xy_tmp_dir = workspace_dir / "tmp" / "xiaoyi"
    xy_tmp_dir.mkdir(parents=True, exist_ok=True)
    return xy_tmp_dir


def get_env_file() -> Path:
    return get_config_dir() / ".env"


def get_config_file() -> Path:
    """Get the config.yaml file path."""
    return get_config_dir() / "config.yaml"


def is_package_installation() -> bool:
    """Check if running from package installation."""
    return _detect_installation_mode()


from jiuwenclaw.log import (  # noqa: E402
    JsonOnlyFormatter,
    LoggingLevels,
    RuntimeLogFormatter,
    SafeRotatingFileHandler,
    SensitiveDataFilter,
    async_logging_enabled,
    configure_asyncio_event_loop_logging,
    format_session_log,
    install_global_exception_logging,
    setup_logger,
    shutdown_logging,
)

setup_logger()
logger = logging.getLogger(__name__)


class AsyncLRUCache:
    """带过期时间的 LRU 缓存（异步并发安全）.

    on_evict: 可选异步回调 ``async (key, value) -> None``，在条目被淘汰/过期/移除时调用，
    用于释放被淘汰值持有的资源（如 SQLite engine、AgentManager cleanup）。
    回调在锁外 await，避免长持锁；回调异常被吞掉并记录日志，不影响缓存正常运作。
    """

    def __init__(
            self,
            max_size: int = 100,
            ttl_seconds: int = 600,
            on_evict: "Callable[[str, Any], Awaitable[None]] | None" = None,
    ) -> None:
        # max_size clamp 到 ≥1：put 依赖 popitem(last=False) 淘汰旧值，
        # 缓存为空时 popitem 会抛 KeyError；0/负值会让首次 put 即崩溃。
        self._cache: OrderedDict[str, tuple[Any, float]] = OrderedDict()
        self._max_size = max(1, int(max_size))
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()
        self._on_evict = on_evict

    async def _safe_evict(self, key: str, value: Any, *, source: str = "") -> None:
        """在锁外执行淘汰回调；异常吞掉不影响调用方.

        ``source`` 标识淘汰来源（``get_expired``/``put_cover``/``put_lru``/
        ``remove``/``clear``/``keys_expired``/``cleanup_expired``），仅用于日志，
        不传给 on_evict 回调（回调签名保持 ``(key, value)``）。
        """
        cb = self._on_evict
        if cb is None:
            return
        if source:
            logger.info("[AsyncLRUCache] evict key=%s source=%s", key, source)
        try:
            await cb(key, value)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[AsyncLRUCache] on_evict failed for key=%s source=%s: %s", key, source, exc)

    async def get(self, key: str) -> Any | None:
        """获取缓存值，如果不存在或已过期则返回 None."""
        evicted: list[tuple[str, Any]] = []
        async with self._lock:
            if key not in self._cache:
                return None

            value, timestamp = self._cache[key]
            if time.time() - timestamp > self._ttl:
                self._cache.pop(key, None)
                evicted.append((key, value))
                result = None
            else:
                self._cache.move_to_end(key)
                result = value
        # 锁外 await 淘汰回调（TTL 过期）
        for ek, ev in evicted:
            await self._safe_evict(ek, ev, source="get_expired")
        return result

    async def get_or_create(
            self, key: str, factory: "Callable[[str], Awaitable[Any]]",
    ) -> tuple[Any, bool]:
        """原子地获取或创建缓存项，消除 get→create→put 之间的 TOCTOU 竞态.

        在同一把锁内完成"检查-创建-缓存"：若 key 不存在或已过期，调用 ``factory(key)``
        创建值并写入缓存；若已存在且未过期，直接返回缓存值。

        Args:
            key: 缓存键。
            factory: 异步工厂函数，仅当需要创建时调用。签名 ``async (key) -> value``。
                factory 在锁内 await，因此**会阻塞其他 get/put**——factory 必须快
                （如已就绪的构造），避免长持锁。耗时的初始化应在工厂外完成。

        Returns:
            ``(value, is_new)``：``value`` 为缓存值；``is_new`` 表示本次是否新建。
        """
        evicted: list[tuple[str, Any, str]] = []  # (key, value, source)
        async with self._lock:
            hit = self._cache.get(key)
            now = time.time()
            if hit is not None:
                value, timestamp = hit
                if now - timestamp <= self._ttl:
                    # 命中且未过期：复用，不触发任何淘汰
                    self._cache.move_to_end(key)
                    return value, False
                # 命中但已过期：按 get_expired 淘汰旧值，下面走新建路径
                self._cache.pop(key, None)
                evicted.append((key, value, "get_expired"))

            # key 不存在或已过期：在锁内创建（factory 会阻塞其他操作，需快）
            new_value = await factory(key)
            # 容量淘汰（与 put 一致：仅在满且非空时 popitem）
            if len(self._cache) >= self._max_size and self._cache:
                ek, ev_tuple = self._cache.popitem(last=False)
                evicted.append((ek, ev_tuple[0], "put_lru"))
            self._cache[key] = (new_value, now)
            result, is_new = new_value, True
        # 锁外 await 淘汰回调（过期清理 / 容量淘汰）
        for ek, ev, src in evicted:
            await self._safe_evict(ek, ev, source=src)
        return result, is_new

    async def put(self, key: str, value: Any) -> None:
        """存入缓存值，如果超过容量则淘汰最久未使用的."""
        evicted: list[tuple[str, Any, str]] = []  # (key, value, source)
        async with self._lock:
            if key in self._cache:
                old = self._cache.pop(key)
                evicted.append((key, old[0], "put_cover"))
            elif len(self._cache) >= self._max_size and self._cache:
                # 容量淘汰：缓存非空时才 popitem（防御 max_size≤0 或空缓存的边界）
                ek, ev_tuple = self._cache.popitem(last=False)
                evicted.append((ek, ev_tuple[0], "put_lru"))

            self._cache[key] = (value, time.time())
        # 锁外 await 淘汰回调（容量淘汰 / 原值覆盖）
        for ek, ev, src in evicted:
            await self._safe_evict(ek, ev, source=src)

    async def remove(self, key: str) -> None:
        """删除缓存项."""
        async with self._lock:
            value = self._cache.pop(key, None)
        if value is not None:
            await self._safe_evict(key, value[0], source="remove")

    async def clear(self) -> None:
        """清空缓存."""
        async with self._lock:
            items = list(self._cache.items())
            self._cache.clear()
        # 锁外逐个 await 淘汰回调
        for ek, ev_tuple in items:
            await self._safe_evict(ek, ev_tuple[0], source="clear")

    def __len__(self) -> int:
        return len(self._cache)

    async def keys(self, *, _evict_source: str = "keys_expired") -> list[str]:
        evicted: list[tuple[str, Any]] = []
        async with self._lock:
            now = time.time()
            for key in [
                k for k, (_, timestamp) in self._cache.items()
                if now - timestamp > self._ttl
            ]:
                ev_tuple = self._cache.pop(key, None)
                if ev_tuple is not None:
                    evicted.append((key, ev_tuple[0]))
            result = list(self._cache.keys())
        # 锁外 await 淘汰回调（TTL 过期清理）
        for ek, ev in evicted:
            await self._safe_evict(ek, ev, source=_evict_source)
        return result

    async def cleanup_expired(self) -> int:
        """清理所有 TTL 过期条目并触发 on_evict 回调，返回清理的条目数.

        与 ``keys()`` 的差异：本方法语义明确为"主动清理过期"，返回清理计数而非
        剩余 key 列表，便于调用方表达"为副作用而调用"的意图，避免忽略返回值的告警。
        """
        before = len(self._cache)
        result = await self.keys(_evict_source="cleanup_expired")  # 标注来源为主动清理
        return before - len(result)


_TOOL_ARGS_LOG_MAX_DEFAULT = 480


def _truncate_tool_args_log_fragment(text: str, *, full_detail: bool) -> str:
    if full_detail or len(text) <= _TOOL_ARGS_LOG_MAX_DEFAULT:
        return text
    return text[:_TOOL_ARGS_LOG_MAX_DEFAULT] + "..."


def _log_tool_args_repair_stage(
        *,
        stage: str,
        before_raw: str,
        outcome: Literal["success", "failed"],
        after_dict: Optional[dict] = None,
        error: Optional[str] = None,
) -> None:
    full_detail = logger.isEnabledFor(logging.DEBUG)
    before_shown = _truncate_tool_args_log_fragment(before_raw, full_detail=full_detail)
    if outcome == "success":
        after_raw = (
            json.dumps(after_dict, ensure_ascii=False)
            if isinstance(after_dict, dict)
            else ""
        )
        after_shown = _truncate_tool_args_log_fragment(after_raw, full_detail=full_detail)
        logger.info(
            "[fix_json_arguments] stage=%s outcome=success before=%s after=%s",
            stage,
            before_shown,
            after_shown,
        )
    else:
        err_shown = _truncate_tool_args_log_fragment(error or "", full_detail=full_detail)
        logger.warning(
            "[fix_json_arguments] stage=%s outcome=failed before=%s error=%s",
            stage,
            before_shown,
            err_shown,
        )


def _fix_missing_quotes(json_str: str) -> str:
    s = json_str.strip()

    s = re.sub(
        r':\s+([A-Za-z]:/[^\{\[]*?)(?=\s*[,\}\]])',
        lambda m: f': "{m.group(1)}"',
        s
    )

    s = re.sub(
        r':\s+(?!"|true|false|null|\d+|{|\[|:|"|[A-Za-z]:/)([^\s,\}\[\]""]+?)(?=\s*[,}\]])',
        lambda m: f': "{m.group(1)}"',
        s
    )

    s = re.sub(
        r'{\s+([a-zA-Z_][a-zA-Z0-9_]*)\s*:',
        r'{"\1":',
        s
    )

    return s


def fix_json_arguments(arguments: str | dict) -> str | dict:
    if not isinstance(arguments, str):
        return arguments

    s = arguments.strip()

    if not s:
        return {}

    try:
        return json.loads(s)
    except json.JSONDecodeError:
        pass

    full_detail = logger.isEnabledFor(logging.DEBUG)

    try:
        import json_repair

        repaired = json_repair.loads(s)
    except Exception as exc:
        _log_tool_args_repair_stage(
            stage="json_repair",
            before_raw=s,
            outcome="failed",
            error=str(exc),
        )
    else:
        if isinstance(repaired, dict):
            _log_tool_args_repair_stage(
                stage="json_repair",
                before_raw=s,
                outcome="success",
                after_dict=repaired,
            )
            return repaired
        _log_tool_args_repair_stage(
            stage="json_repair",
            before_raw=s,
            outcome="failed",
            error=f"repaired_not_object:{type(repaired).__name__}",
        )

    fixed = _fix_missing_quotes(s)
    if fixed != s:
        try:
            result = json.loads(fixed)
        except json.JSONDecodeError as exc:
            _log_tool_args_repair_stage(
                stage="rule_fix",
                before_raw=s,
                outcome="failed",
                error=str(exc),
            )
        else:
            _log_tool_args_repair_stage(
                stage="rule_fix",
                before_raw=s,
                outcome="success",
                after_dict=result,
            )
            return result
    else:
        _log_tool_args_repair_stage(
            stage="rule_fix",
            before_raw=s,
            outcome="failed",
            error="no_structural_change_from_rules",
        )

    before_final = _truncate_tool_args_log_fragment(s, full_detail=full_detail)
    logger.warning(
        "[fix_json_arguments] outcome=failed_all_stages before=%s error=all_repair_attempts_exhausted",
        before_final,
    )
    return {}


# ===========================================================================
# 文件传输工具函数
# ===========================================================================

@dataclass
class TransferProgress:
    """文件传输进度状态（Gateway 和 AgentServer 共用）."""
    transfer_id: str
    filename: str
    file_size: int
    total_chunks: int
    received_chunks: int = 0
    sha256: str = ""
    chunks: dict[int, bytes] = field(default_factory=dict)
    start_time: float = field(default_factory=time.time)
    mime_type: str = ""
    session_id: str = ""
    channel_id: str = ""  # Gateway 端需要，AgentServer 端可选


def safe_filename(filename: str) -> str:
    """生成安全的文件名.

    移除路径分隔符和可能导致跨平台文件系统问题的字符，
    防止路径遍历攻击。
    """
    # 移除路径分隔符，防止路径遍历
    safe = filename.replace("/", "_").replace("\\", "_")
    # 只保留字母、数字和安全的标点符号
    safe = "".join(c for c in safe if c.isalnum() or c in "._- ")
    return safe or "unnamed_file"


def guess_mime_type(filename: str) -> str:
    """根据文件扩展名猜测 MIME 类型.

    使用 Python 标准 mimetypes 库，支持大多数常见文件类型。
    """
    mime_type, _ = mimetypes.guess_type(filename)
    return mime_type or "application/octet-stream"


def build_default_headers(session_id="") -> dict[str, str] | None:
    """构建 HTTP 请求的默认头。

    从环境变量 ``default_headers`` 读取 JSON 对象作为基础头；
    若 ``SANDBOX_ENABLE`` 为 true，则通过 open_ability_utils 获取沙箱认证头。
    """
    default_headers = os.getenv("default_headers", None)
    try:
        default_headers = json.loads(default_headers) if default_headers else None
    except json.decoder.JSONDecodeError:
        default_headers = None

    sandbox_enable = os.getenv("SANDBOX_ENABLE", "").strip().lower()
    if sandbox_enable == "true":
        from jiuwenclaw.agentserver.open_ability_utils import get_oa_auth_headers
        oa_headers = get_oa_auth_headers(session_id=session_id)
        oa_headers["Accept"] = "text/event-stream"
        if default_headers:
            default_headers.update(oa_headers)
        else:
            default_headers = oa_headers

    return default_headers
