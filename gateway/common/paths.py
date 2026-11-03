# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 侧路径解析（``gateway.common.paths`` 的路径子集副本）。

Gateway 与母仓共用同一份用户工作区与 ``config`` 目录（配置文件共享），因此这里
的返回值必须与母仓实现保持一致；差异仅在于 Gateway 不再通过 import 母仓
``utils`` 触发其模块级 ``setup_logger()``。

缓存为模块级：若同进程内有人调用母仓的 ``set_user_home`` 改变主目录，本模块
缓存不会跟随。生产路径由 ``--dotenv/--name`` 早期设置环境变量
（``JIUWENSWARM_HOME`` / ``JIUWENSWARM_DATA_DIR``）驱动，双方首次解析结果一致。
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# gateway.log 独立目录开关（与母仓同名的环境变量）
_GATEWAY_LOG_DIR_ENV = "AGENTOS_GATEWAY_LOG_DIR"


# --- 用户主目录 / 工作区 ---
_user_home: Path | None = None
_workspace_base_dir: Path | None = None


def get_user_home() -> Path:
    """获取用户主目录：缓存 > ``JIUWENSWARM_HOME`` > ``Path.home()``。"""
    global _user_home
    if _user_home is not None:
        return _user_home
    env_home = os.getenv("JIUWENSWARM_HOME")
    if env_home:
        _user_home = Path(env_home)
        return _user_home
    _user_home = Path.home()
    return _user_home


def set_user_home(path: Path, initialized: bool = False) -> None:
    """设置自定义用户主目录；``initialized=False`` 时同时清空路径缓存。"""
    global _user_home, _initialized, _config_dir, _workspace_dir, _root_dir
    _user_home = Path(path)
    if initialized:
        return
    _initialized = False
    _config_dir = None
    _workspace_dir = None
    _root_dir = None


def get_user_workspace_dir() -> Path:
    """获取用户工作区：缓存 > ``JIUWENSWARM_DATA_DIR`` > ``~/.jiuwenswarm``。"""
    global _workspace_base_dir
    if _workspace_base_dir is not None:
        return _workspace_base_dir
    env_workspace = os.getenv("JIUWENSWARM_DATA_DIR")
    if env_workspace:
        _workspace_base_dir = Path(env_workspace)
        return _workspace_base_dir
    _workspace_base_dir = get_user_home() / ".jiuwenswarm"
    return _workspace_base_dir


# --- 包内资源与配置目录 ---
_config_dir: Path | None = None
_workspace_dir: Path | None = None
_root_dir: Path | None = None
_initialized: bool = False


def _find_package_root() -> Path | None:
    """向上查找含 ``resources/config.yaml`` 的 jiuwenswarm 包目录。

    源码布局命中 ``<repo>/jiuwenswarm/resources``；wheel 共同安装时命中
    ``site-packages/jiuwenswarm/resources``。Gateway 自身不带该资源目录。
    """
    for parent in Path(__file__).resolve().parents:
        if (parent / "resources" / "config.yaml").exists():
            return parent
        nested = parent / "jiuwenswarm" / "resources" / "config.yaml"
        if nested.exists():
            return nested.parent.parent
    return None


def _resolve_paths() -> None:
    """解析并缓存 config / workspace / root 目录。"""
    global _initialized, _config_dir, _workspace_dir, _root_dir

    if _initialized:
        return

    workspace_dir = get_user_workspace_dir()
    user_config_dir = workspace_dir / "config"
    user_workspace_dir = workspace_dir / "agent" / "workspace"
    if user_config_dir.exists():
        # 已初始化的用户工作区优先，保证源码运行与安装包运行路径一致
        _root_dir = workspace_dir
        _config_dir = user_config_dir
        _workspace_dir = user_workspace_dir
    else:
        package_root = _find_package_root()
        if package_root is not None:
            res = package_root / "resources"
            _root_dir = package_root.parent
            _config_dir = res
            _workspace_dir = res / "agent" / "workspace"
        else:
            _root_dir = Path(__file__).resolve().parents[2]
            _config_dir = _root_dir / "config"
            _workspace_dir = _root_dir / "jiuwenswarm" / "resources" / "agent" / "workspace"
        _workspace_dir.mkdir(parents=True, exist_ok=True)

    _initialized = True


def get_config_dir() -> Path:
    """获取配置目录（与母仓共享同一份 config）。"""
    _resolve_paths()
    return _config_dir


def get_workspace_dir() -> Path:
    """获取 agent workspace 目录。"""
    _resolve_paths()
    return _workspace_dir


def get_root_dir() -> Path:
    """获取根目录（已初始化时为用户工作区）。"""
    _resolve_paths()
    return _root_dir


def get_config_file() -> Path:
    """获取 ``config.yaml`` 路径。"""
    return get_config_dir() / "config.yaml"


def get_env_file() -> Path:
    """获取 ``.env`` 路径。"""
    return get_config_dir() / ".env"


def env_url(name: str, default: str) -> str:
    """读取环境变量中的 URL，未设置或空白时回退默认值。"""
    return os.environ.get(name, "").strip() or default


# --- agent 工作区子目录 ---
def get_agent_root_dir() -> Path:
    return get_user_workspace_dir() / "agent"


def get_agent_home_dir() -> Path:
    return get_agent_root_dir() / "home"


def get_agent_workspace_dir() -> Path:
    """DeepAgent 标准工作区：``~/.jiuwenswarm/agent/workspace``。"""
    return get_agent_root_dir() / "workspace"


def get_agent_memory_dir() -> Path:
    return get_agent_workspace_dir() / "memory"


def get_agent_skills_dir() -> Path:
    return get_agent_workspace_dir() / "skills"


def get_agent_sessions_dir() -> Path:
    return get_agent_root_dir() / "sessions"


def get_deepagent_user_md_path() -> Path:
    return get_agent_workspace_dir() / "USER.md"


def get_interactions_dir() -> Path:
    return get_agent_workspace_dir() / "interactions"


def get_cron_jobs_path() -> Path:
    """``agent/home/cron_jobs.json`` 的规范路径（禁止业务代码硬编码）。"""
    return get_agent_home_dir() / "cron_jobs.json"


def get_xy_tmp_dir() -> Path:
    xy_tmp_dir = get_user_workspace_dir() / "tmp" / "xiaoyi"
    xy_tmp_dir.mkdir(parents=True, exist_ok=True)
    return xy_tmp_dir


# --- checkpoint / 日志目录 ---
_legacy_migration_done: bool = False


def _migrate_legacy_checkpoint_and_logs() -> None:
    """一次性迁移 ``.checkpoint`` / ``.logs`` 到 ``agent/`` 下。"""
    global _legacy_migration_done
    if _legacy_migration_done:
        return
    _legacy_migration_done = True

    workspace = get_user_workspace_dir()
    agent_root = workspace / "agent"

    for name in (".checkpoint", ".logs"):
        legacy = workspace / name
        new_path = agent_root / name
        if legacy.exists() and not new_path.exists():
            agent_root.mkdir(parents=True, exist_ok=True)
            shutil.move(str(legacy), str(new_path))


def get_checkpoint_dir() -> Path:
    _migrate_legacy_checkpoint_and_logs()
    return get_agent_root_dir() / ".checkpoint"


def get_logs_dir() -> Path:
    _migrate_legacy_checkpoint_and_logs()
    return get_agent_root_dir() / ".logs"


def get_gateway_log_dir() -> Optional[Path]:
    """获取 gateway 独立日志目录；未配置 ``AGENTOS_GATEWAY_LOG_DIR`` 时返回 None。"""
    env_dir = os.getenv(_GATEWAY_LOG_DIR_ENV, "").strip()
    if env_dir:
        return Path(env_dir)
    return None


# --- git 分支 ---
_GIT_BRANCH_CACHE: dict[str, tuple[float, str]] = {}
_GIT_BRANCH_TTL_SECONDS = 5.0


def resolve_git_branch(project_dir: str | None) -> str:
    """返回 ``project_dir`` 当前 git 分支，取不到时返回哨兵 ``"HEAD"``。"""
    if not project_dir or not os.path.isdir(project_dir):
        return "HEAD"
    now = time.time()
    cached = _GIT_BRANCH_CACHE.get(project_dir)
    if cached and now - cached[0] < _GIT_BRANCH_TTL_SECONDS:
        return cached[1]
    branch = "HEAD"
    git_bin = shutil.which("git")
    if git_bin:
        try:
            result = subprocess.run(
                [git_bin, "rev-parse", "--abbrev-ref", "HEAD"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=5,
                cwd=project_dir,
            )
            if result.returncode == 0:
                branch = result.stdout.strip() or "HEAD"
        except Exception:
            branch = "HEAD"
    _GIT_BRANCH_CACHE[project_dir] = (now, branch)
    return branch


# --- 运行时环境与工作区准备 ---
def apply_free_search_runtime_defaults() -> None:
    """未显式配置时关闭 free-search 引擎（.env / 配置面板 / shell 优先）。"""
    os.environ.setdefault("FREE_SEARCH_DDG_ENABLED", "false")
    os.environ.setdefault("FREE_SEARCH_BING_ENABLED", "false")


def _is_windows_frozen_bundle() -> bool:
    return sys.platform == "win32" and bool(getattr(sys, "frozen", False))


def cleanup_stale_openjiuwen_descs() -> None:
    """删除布局迁移遗留的扁平 OpenJiuwen 工具描述文件（幂等）。"""
    if _is_windows_frozen_bundle():
        logger.info(
            "[Cleanup] Skipping OpenJiuwen description cleanup in frozen Windows "
            "bundle; the installer performs upgrade cleanup."
        )
        return

    try:
        import openjiuwen
    except ModuleNotFoundError as exc:
        if exc.name == "openjiuwen":
            return
        raise

    package_file = getattr(openjiuwen, "__file__", None)
    if not package_file:
        return

    descs_dir = (
        Path(package_file).parent / "agent_teams" / "tools" / "locales" / "descs"
    )
    if not descs_dir.is_dir():
        return

    for lang_dir in sorted(path for path in descs_dir.iterdir() if path.is_dir()):
        nested_stems = set()
        for desc_path in lang_dir.rglob("*.md"):
            if desc_path.parent == lang_dir:
                continue
            if "fragments" in desc_path.relative_to(lang_dir).parts:
                continue
            nested_stems.add(desc_path.stem)

        for flat_md in sorted(lang_dir.glob("*.md")):
            if flat_md.stem not in nested_stems:
                continue
            try:
                flat_md.unlink()
                logger.info(
                    f"[Cleanup] Removed stale flat OpenJiuwen description: {flat_md}"
                )
            except FileNotFoundError:
                continue
            except OSError as exc:
                raise RuntimeError(
                    "Failed to remove stale OpenJiuwen description "
                    f"'{flat_md}'. Ensure the Python environment is writable "
                    "or reinstall OpenJiuwen in a clean environment."
                ) from exc


def prepare_runtime_workspace(*, cleanup_stale_descs: bool = True) -> None:
    """Gateway 独立启动时的最小工作区准备。

    完整初始化（配置模板迁移、内置 skills、MCP 种子）由母仓 supervisor /
    ``jiuwenswarm-init`` 完成；Gateway 作为子进程启动时带
    ``JIUWENSWARM_RUNTIME_WORKSPACE_READY=1``，不会走到这里。本函数只保证
    运行期必需的目录存在，不修改共享配置。
    """
    if cleanup_stale_descs:
        cleanup_stale_openjiuwen_descs()

    workspace_dir = get_user_workspace_dir()
    for path in (workspace_dir / "agent" / "workspace", workspace_dir / "agent" / "home"):
        path.mkdir(parents=True, exist_ok=True)


__all__ = [
    "apply_free_search_runtime_defaults",
    "cleanup_stale_openjiuwen_descs",
    "env_url",
    "get_agent_home_dir",
    "get_agent_memory_dir",
    "get_agent_root_dir",
    "get_agent_sessions_dir",
    "get_agent_skills_dir",
    "get_agent_workspace_dir",
    "get_checkpoint_dir",
    "get_config_dir",
    "get_config_file",
    "get_cron_jobs_path",
    "get_deepagent_user_md_path",
    "get_env_file",
    "get_gateway_log_dir",
    "get_interactions_dir",
    "get_logs_dir",
    "get_root_dir",
    "get_user_home",
    "get_user_workspace_dir",
    "get_workspace_dir",
    "get_xy_tmp_dir",
    "logger",
    "prepare_runtime_workspace",
    "resolve_git_branch",
    "set_user_home",
]
