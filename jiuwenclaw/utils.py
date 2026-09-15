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
- <root>/agent/.logs（gateway.log / channel.log / agent_server.log / full.log）

内置模板位于包内 ``jiuwenclaw/resources/``（含 ``agent/`` 下各技能模板以及 ``skills_state.json``）。
"""

import asyncio
import copy
import datetime
import json
import logging
import mimetypes
import os
import queue as _queue
import re
import shutil
import sys
import threading
import time
import contextlib
from collections import OrderedDict
from collections.abc import Hashable
from dataclasses import dataclass, field
from logging.handlers import BaseRotatingHandler, QueueHandler, QueueListener
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Dict, Literal, Optional

import yaml
from ruamel.yaml import YAML
from ruamel.yaml.comments import CommentedMap

from jiuwenclaw.local_env_config import SPAWN_ENV_KEYS, get_local_config

# 尝试导入 pythonjsonlogger（用于 JSON 格式化输出）
try:
    from pythonjsonlogger import jsonlogger
except ImportError:
    jsonlogger = None

logger = logging.getLogger(__name__)


def merge_template_with_override(
    template: dict[str, Any],
    override: dict[str, Any],
) -> dict[str, Any]:
    """模板默认值 + 用户 override；用户键覆盖模板。override 中独有的顶层键保留。"""
    out: dict[str, Any] = {}
    for key, tmpl_val in template.items():
        if key not in override:
            out[key] = copy.deepcopy(tmpl_val)
        elif isinstance(tmpl_val, dict) and isinstance(override.get(key), dict):
            out[key] = merge_template_with_override(tmpl_val, override[key])
        else:
            out[key] = override[key]
    for key, over_val in override.items():
        if key not in template:
            out[key] = copy.deepcopy(over_val)
    return out


def load_yaml_dict(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    return data if isinstance(data, dict) else {}


def resolve_shipped_template_config_path() -> Path:
    """包内 shipped 模板：jiuwenclaw/resources/config.yaml。"""
    return Path(__file__).resolve().parent / "resources" / "config.yaml"


def resolve_env_vars(value: Any) -> Any:
    """递归解析配置中的环境变量替换语法 ${VAR:-default}.

    进程共享键（``SPAWN_ENV_KEYS``）只读 ``os.environ``；智能体隔离键只读
    tip/ns（``get_local_config``），禁止回退到 bare ``os.environ``，避免单进程
    多 agent 时串读 spawn 泄漏的凭证（如 API_KEY）。
    """
    if isinstance(value, str):
        pattern = r'\$\{([^:}]+)(?::-([^}]*))?\}'

        def replace_env(match):
            var_name = match.group(1)
            default = match.group(2)
            if var_name in SPAWN_ENV_KEYS:
                current = os.environ.get(var_name)
            else:
                current = get_local_config(var_name)
            if default is not None:
                if current is None or current == "":
                    return default
                return current
            return current if current is not None else ""

        return re.sub(pattern, replace_env, value)
    if isinstance(value, dict):
        return {k: resolve_env_vars(v) for k, v in value.items()}
    if isinstance(value, list):
        return [resolve_env_vars(item) for item in value]
    return value


_LOG_FILE_MAX_BYTES = 20 * 1024 * 1024
_LOG_FILE_BACKUP_COUNT = 20


@dataclass
class LoggingLevels:
    """Container for logging level configuration."""
    logger: int
    console: int
    gateway: int
    channel: int
    agent_server: int
    full: int


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
    service_id: str = ""
    agent_id: str = ""


@contextlib.contextmanager
def inter_process_lock(
    lock_file_path: Path,
    timeout: float = 30.0,
    poll_interval: float = 0.1,
    stale_timeout: float = 120.0,
):
    """
    跨进程锁，利用 os.O_CREAT | os.O_EXCL 的原子性。

    用于保护多进程并发访问共享资源（如工作目录初始化）的场景。

    Args:
        lock_file_path: 锁文件路径
        timeout: 获取锁的超时时间（秒），超时后抛出 TimeoutError
        poll_interval: 轮询间隔（秒），用于等待锁释放
        stale_timeout: 僵尸锁文件的过期时间（秒），超过此时间的锁文件将被清理

    Example:
        >>> lock_file = workspace_dir / ".init.lock"
        >>> with inter_process_lock(lock_file, timeout=60.0):
        >>>     # 临界区代码，只有一个进程可以执行
        >>>     prepare_workspace(overwrite=False)

    Note:
        - 僵尸锁清理：如果锁文件存在且超过 stale_timeout 时间未更新，将被自动清理
          （防止进程崩溃后遗留的死锁）
        - 锁文件内容：包含当前进程 PID 和获取时间，用于诊断
        - 异常安全：即使临界区抛出异常，锁文件也会在 finally 中被清理
    """
    # Step 1: 检查并清理僵尸锁文件
    if lock_file_path.exists():
        try:
            file_age = time.time() - lock_file_path.stat().st_mtime
            if file_age > stale_timeout:
                # 锁文件过期，可能是上一个进程崩溃遗留的僵尸锁
                lock_file_path.unlink()
                logger.warning(
                    f"Removed stale lock file (age={file_age:.1f}s > stale_timeout={stale_timeout}s): {lock_file_path}"
                )
        except OSError as e:
            logger.warning(f"Failed to check/remove stale lock file: {e}")

    # Step 2: 尝试获取锁
    start_time = time.time()
    fd = None
    first_attempt = True

    while True:
        try:
            # 尝试排他性地创建文件
            fd = os.open(str(lock_file_path), os.O_CREAT | os.O_EXCL | os.O_RDWR, mode=0o666)
            # 成功获取锁，写入当前进程 PID 和时间戳用于诊断
            if fd is not None:
                pid_info = f"{os.getpid()}\n{time.time()}\n".encode()
                os.write(fd, pid_info)
            logger.info(
                f"Lock acquired (waited={time.time() - start_time:.2f}s, pid={os.getpid()}): {lock_file_path}"
            )
            break
        except FileExistsError:
            # 锁已被其他进程占用
            if first_attempt:
                logger.info(
                    f"Lock held by another process, waiting (max_timeout={timeout}s): {lock_file_path}"
                )
                first_attempt = False

            elapsed = time.time() - start_time
            if elapsed > timeout:
                raise TimeoutError(
                    f"获取跨进程锁超时 ({elapsed:.1f}s > {timeout}s): {lock_file_path}"
                ) from None
            time.sleep(poll_interval)

    try:
        yield
    finally:
        # Step 3: 释放锁：关闭文件描述符并删除锁文件
        if fd is not None:
            os.close(fd)
            try:
                os.remove(str(lock_file_path))
                logger.info(f"Lock released: {lock_file_path}")
            except OSError as e:
                logger.warning(f"Failed to remove lock file: {e}")


def ensure_workspace_initialized(
    component_name: str = "App",
    timeout: float = 60.0,
    stale_timeout: float = 120.0,
) -> None:
    """
    确保工作区已初始化，使用跨进程锁保护并发访问。

    用于多入口应用（app.py、app_agentserver.py、app_gateway.py）的启动初始化。
    在并发启动场景下，多个进程可能同时尝试初始化工作区，此函数确保只有一个进程执行初始化。

    Args:
        component_name: 组件名称，用于日志标识（如 "App", "AgentServer", "Gateway"）
        timeout: 获取锁的超时时间（秒），超时后抛出 TimeoutError
        stale_timeout: 僵尸锁文件的过期时间（秒），超过此时间的锁文件将被清理

    Example:
        >>> # 在入口文件中调用
        >>> ensure_workspace_initialized(component_name="App")

    Note:
        - 初始化条件：config.yaml 不存在，或旧 workspace 存在但新不存在（迁移场景）
        - 始终清理 Team 旧版本遗留文件（幂等操作）
        - 异常安全：即使初始化失败，锁也会被正确释放
    """
    _workspace_dir = get_user_workspace_dir()
    _config_file = _workspace_dir / "config" / "config.yaml"
    _multi_tenant_workspace = get_multi_tenant_user_workspace_dir("default", "default")
    if _multi_tenant_workspace:
        _new_workspace = _multi_tenant_workspace / "agent" / "jiuwenclaw_workspace"
    else:
        _new_workspace = _workspace_dir / "agent" / "jiuwenclaw_workspace"
    _old_workspace = _workspace_dir / "agent" / "workspace"

    # 始终清理 Team 旧版本遗留文件（幂等操作，在 prepare_workspace 之前执行）
    cleanup_team_files(_workspace_dir)

    # 使用跨进程锁保护初始化过程，防止并发竞争
    _lock_file = _workspace_dir / ".init.lock"
    logger.info(f"[{component_name}] Acquiring workspace init lock: {_lock_file}")

    with inter_process_lock(_lock_file, timeout=timeout, stale_timeout=stale_timeout):
        if not _config_file.exists() or (_old_workspace.exists() and not _new_workspace.exists()):
            logger.info(
                f"[{component_name}] Workspace initialization required "
                f"(config_exists={_config_file.exists()}, "
                f"legacy_migration={_old_workspace.exists() and not _new_workspace.exists()})"
            )
            prepare_workspace(overwrite=False)
            logger.info(f"[{component_name}] Workspace initialization completed")
        else:
            logger.info(f"[{component_name}] Workspace already initialized, updating config")
            update_config()
            cleanup_legacy_flat_agent_dir(_workspace_dir)
            logger.info(f"[{component_name}] Config update completed")

    logger.info(f"[{component_name}] Workspace init lock released, proceeding to start")


class SafeRotatingFileHandler(BaseRotatingHandler):
    """Safe rotating file handler"""

    def __init__(self, filename, maxBytes=0, backupCount=0, encoding=None,
                 delay=False, errors=None):
        """Initialize the handler."""
        super().__init__(filename, 'a', encoding, errors)
        self.max_bytes = maxBytes
        self.backup_count = backupCount
        self._current_filename = filename

        if delay:
            self.stream = None

    def shouldRollover(self, record):
        """
        Determine if rollover should occur.

        Returns True if the log file size exceeds maxBytes.
        """
        if self.stream is None:
            return False
        if self.max_bytes > 0:
            msg = "%s\n" % self.format(record)
            self.stream.seek(0, 2)  # Seek to end of file
            if self.stream.tell() + len(msg) >= self.max_bytes:
                return True
        return False

    def doRollover(self):
        """
        Perform log rotation to keep app.log as the active log file.
        """
        base_path = Path(self.baseFilename)

        timestamp = datetime.now(tz=timezone.utc).strftime("%Y%m%d_%H%M%S")
        backup_filename = base_path.parent / f"{base_path.stem}_{timestamp}{base_path.suffix}"

        try:
            if base_path.exists():
                shutil.copy2(base_path, backup_filename)
        except OSError as e:
            print(f"WARNING: Could not copy log file to backup: {e}", file=sys.stderr)

        # Clean up old backup files
        self._cleanup_old_backups()

        try:
            if self.stream:
                self.stream.seek(0)  # Seek to beginning
                self.stream.truncate(0)  # Truncate to 0 bytes
        except OSError as e:
            print(f"WARNING: Could not truncate log file: {e}", file=sys.stderr)

    def _cleanup_old_backups(self):
        """
        Remove old backup files if they exceed backupCount.

        Backup files are sorted by modification time (oldest first).
        """
        if self.backup_count <= 0:
            return

        try:
            base_path = Path(self.baseFilename)
            log_dir = base_path.parent

            backup_files = []
            for f in log_dir.glob(f"{base_path.stem}_*{base_path.suffix}"):
                if f.is_file() and f != base_path:
                    backup_files.append(f)

            # Sort by modification time (oldest first)
            backup_files.sort(key=lambda x: x.stat().st_mtime)

            # Remove excess files
            files_to_delete = len(backup_files) - self.backup_count
            if files_to_delete > 0:
                for f in backup_files[:files_to_delete]:
                    try:
                        f.unlink()
                    except OSError as e:
                        print(f"WARNING: Could not delete old log file {f}: {e}", file=sys.stderr)
        except Exception as e:
            print(f"WARNING: Error during backup cleanup: {e}", file=sys.stderr)


def _parse_log_level(name: str, default: int = logging.INFO) -> int:
    """Parse level name to logging module constant."""
    if not name or not isinstance(name, str):
        return default
    return getattr(logging, name.strip().upper(), default)


def _log_component_from_logger_name(name: str) -> str:
    """按 ``logging.getLogger(__name__)`` 的 logger 名划分 gateway / channel / agent_server / permissions。"""
    if name.startswith("jiuwenclaw.channel"):
        return "channel"
    if name.startswith("jiuwenclaw.agentserver.permissions.checker"):
        return "permissions"
    if name.startswith("jiuwenclaw.agentserver"):
        return "agent_server"
    if name.startswith("jiuwenclaw.utils"): 
        return "agent_server"
    return name


class _ComponentNameFilter(logging.Filter):
    """仅放行指定组件（由 logger 名判定）的日志记录。"""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        # 优先使用 extra 中的 component 标记，允许精确控制特定日志行
        if hasattr(record, 'component'):
            return record.component == self.component
        return _log_component_from_logger_name(record.name) == self.component


class _CompositeFilter(logging.Filter):
    """组合多个过滤器，任一通过即放行"""

    def __init__(self, filters: list[logging.Filter]) -> None:
        super().__init__()
        self.filters = filters

    def filter(self, record: logging.LogRecord) -> bool:
        return any(f.filter(record) for f in self.filters)


def _load_logging_config_from_yaml() -> dict[str, Any]:
    """读取合并并解析环境变量后的 ``logging`` 段（包内模板 + 用户 override）。

    逻辑与本模块中的 ``merge_template_with_override`` / ``resolve_env_vars`` 一致；
    放在 ``utils`` 内以便 ``setup_logger`` 在导入 ``jiuwenclaw.config`` 之前即可执行。
    """
    try:
        template = load_yaml_dict(resolve_shipped_template_config_path())
        override = load_yaml_dict(get_config_file())
        merged = merge_template_with_override(template, override)
        raw = merged.get("logging")
        if isinstance(raw, dict):
            return resolve_env_vars(raw)
    except Exception as e:
        logger.error(f"load logging config failed, caused by={e}")
    return {}


def _resolve_logging_levels(
    log_level_override: Optional[str],
) -> LoggingLevels:
    """返回日志级别配置。"""
    cfg = _load_logging_config_from_yaml()
    base = _parse_log_level(str(cfg.get("level", "INFO")))

    def _coerce(key: str) -> int:
        if key in cfg and cfg[key] is not None:
            return _parse_log_level(str(cfg[key]), base)
        return base

    console = _coerce("console_level")
    env_console = os.getenv("LOG_LEVEL")
    if env_console:
        console = _parse_log_level(env_console, console)

    gateway = _coerce("gateway")
    channel = _coerce("channel")
    agent_server = _coerce("agent_server")
    full = _coerce("full")

    # ``LOG_LEVEL`` intentionally controls console output only.  The prefixed
    # setting is an explicit process-level override for every handler, which
    # lets an embedding host enable diagnostic file traces without mutating
    # the user's config.yaml.
    effective_override = (
        log_level_override if log_level_override is not None else os.getenv("JIUWENCLAW_LOG_LEVEL")
    )
    if effective_override is not None:
        v = _parse_log_level(effective_override)
        console = gateway = channel = agent_server = full = v
        logger_level = v
    else:
        logger_level = min(gateway, channel, agent_server, full)

    return LoggingLevels(logger_level, console, gateway, channel, agent_server, full)


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


def cleanup_legacy_flat_agent_dir(workspace_dir: Path) -> None:
    """Remove legacy root-level agent data after the multi-tenant workspace exists."""
    legacy_agent = workspace_dir / "agent"
    if not legacy_agent.is_dir():
        return

    new_agent_root = workspace_dir / "service_default" / "agent_default" / "agent"
    new_deep = new_agent_root / "jiuwenclaw_workspace"
    if not new_deep.is_dir():
        return

    try:
        same_agent_root = legacy_agent.resolve() == new_agent_root.resolve()
    except OSError as e:
        logger.warning("[Cleanup] Skip legacy flat agent cleanup (resolve failed): %s", e)
        return

    if same_agent_root:
        return

    try:
        shutil.rmtree(legacy_agent)
        logger.info("[Cleanup] Removed legacy flat agent directory: %s", legacy_agent)
    except OSError as e:
        logger.warning("[Cleanup] Failed to remove legacy flat agent directory %s: %s", legacy_agent, e)


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

    # 多租户布局下删除遗留的根目录平铺 ``<root>/agent``（与新路径非同一目录时）
    cleanup_legacy_flat_agent_dir(workspace_dir)

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


def deep_merge_dicts(base: dict, overlay: dict) -> dict:
    """将 overlay 字典深度合并到 base 字典之上，返回新字典。

    规则（overlay 覆盖 base）：
    - base 有、overlay 没有 → 保留 base 值
    - 两边都有且都是 dict → 递归合并
    - overlay 有值 → overlay 覆盖 base

    Args:
        base: 基础完整配置（如从 get_config() 读取的完整 config.yaml）。
        overlay: 增量覆盖配置（如 Gateway 传来的部分配置）。

    Returns:
        合并后的新字典，不修改 base 和 overlay。
    """
    result = dict(base)
    for key, overlay_val in overlay.items():
        base_val = result.get(key)
        if isinstance(base_val, dict) and isinstance(overlay_val, dict):
            result[key] = deep_merge_dicts(base_val, overlay_val)
        else:
            result[key] = overlay_val
    return result


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


def _read_template_version_value(template_path: Path) -> Any:
    """读取模板 config.yaml 顶层的 ``version``（缺省 ``1.0``）；有定义则按 YAML 解析结果原样采用。"""
    if not template_path.exists():
        return 1.0
    try:
        rt = YAML()
        rt.preserve_quotes = True
        with open(template_path, "r", encoding="utf-8") as f:
            tpl = rt.load(f)
        if isinstance(tpl, dict) and tpl.get("version") is not None:
            return tpl["version"]
    except OSError as e:
        logger.warning("Failed to read template version from %s: %s", template_path, e)
    return 1.0


def _write_initial_user_override_config(template_src: Path, dest: Path) -> None:
    """首次初始化用户目录时写入稀疏 override（仅 ``version``，取自模板）。"""
    version_val = _read_template_version_value(template_src)
    rt = YAML()
    rt.preserve_quotes = True
    rt.default_flow_style = False
    rt.indent(mapping=2, sequence=4, offset=2)
    rt.width = 4096
    data = CommentedMap()
    data["version"] = version_val
    dest.parent.mkdir(parents=True, exist_ok=True)
    with open(dest, "w", encoding="utf-8") as f:
        rt.dump(data, f)


def migrate_legacy_user_config_if_needed() -> None:
    """无 ``version`` 的旧版完整 config 迁移为稀疏 override：仅保留 permissions + version。

    已有 ``version`` 的用户文件不修改。
    """
    cfg_path = get_config_file()
    if not cfg_path.exists():
        return
    try:
        rt = YAML()
        rt.preserve_quotes = True
        rt.default_flow_style = False
        rt.indent(mapping=2, sequence=4, offset=2)
        rt.width = 4096
        with open(cfg_path, "r", encoding="utf-8") as f:
            data = rt.load(f)
        if data is None:
            data = {}
        if not isinstance(data, dict):
            return
        ver = data.get("version")
        if ver is not None and str(ver).strip() != "":
            return

        package_root = _find_package_root()
        tpl_file = (package_root / "resources" / "config.yaml") if package_root else None
        version_val = _read_template_version_value(tpl_file) if tpl_file else 1.0

        new_data = CommentedMap()
        if "permissions" in data:
            new_data["permissions"] = data["permissions"]
        new_data["version"] = version_val

        with open(cfg_path, "w", encoding="utf-8") as f:
            rt.dump(new_data, f)
        logger.info(
            "[jiuwenclaw] migrated legacy config.yaml to sparse override (schema version %s)",
            version_val,
        )
    except OSError as e:
        logger.warning("[jiuwenclaw] legacy config migration failed: %s", e)


def update_config():
    migrate_legacy_user_config_if_needed()

    package_root = _find_package_root()
    if not package_root:
        raise RuntimeError("package root not found")

    workspace_dir = get_user_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)

    # ----- config：稀疏 override 模式不再将模板合并写入用户文件 -----
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


def prepare_workspace(
    overwrite: bool = True,
    preferred_language: Optional[str] = None,
) -> None:
    package_root = _find_package_root()
    if not package_root:
        raise RuntimeError("package root not found")

    workspace_dir = get_user_workspace_dir()
    workspace_dir.mkdir(parents=True, exist_ok=True)
    migrate_legacy_user_config_if_needed()

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
        _write_initial_user_override_config(config_yaml_src, config_yaml_dest)

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

    from jiuwenclaw.config import set_preferred_language_in_config_file

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
    try:
        from jiuwenclaw.agentserver.tenant_context import get_bound_jiuwenclaw_workspace

        bound = get_bound_jiuwenclaw_workspace()
        if bound is not None:
            return bound
    except ImportError:
        logger.debug("tenant_context unavailable for workspace bind", exc_info=True)
    return get_agent_root_dir() / "jiuwenclaw_workspace"


def get_service_root_dir(service_id: str = "default") -> Path:
    """Get the service-level directory path.

    多租户架构下，service 级别存放共享数据（如日志）。
    Path: ~/.jiuwenclaw/service_{service_id}/

    Args:
        service_id: 服务 ID，默认为 "default"
    """
    return get_user_workspace_dir() / f"service_{service_id}"


def get_gateway_dir() -> Path:
    """Get the Gateway-scoped data directory under ``JIUWENCLAW_DATA_DIR``.

    Path: ``{JIUWENCLAW_DATA_DIR}/.gateway``
    Used for process-wide Gateway state (e.g. SessionMap), not per-agent data.
    """
    path = get_user_workspace_dir() / ".gateway"
    path.mkdir(parents=True, exist_ok=True)
    return path


def get_agent_root_dir() -> Path:
    """Get the agent root directory path (multi-tenant default).

    单租户作为多租户的默认特例，返回默认多租户路径。
    Path: ~/.jiuwenclaw/service_default/agent_default/agent/
    """
    try:
        from jiuwenclaw.agentserver.tenant_context import get_bound_agent_root

        bound = get_bound_agent_root()
        if bound is not None:
            return bound
    except ImportError:
        logger.debug("tenant_context unavailable for agent root bind", exc_info=True)
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


def resolve_tenant_env_ns(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> tuple[str, str]:
    """Resolve ``(service_id, agent_id)``: explicit pair > bound env_ns > TypeError."""
    from jiuwenclaw.local_env_config import get_bound_agent_env_ns

    if service_id is not None or agent_id is not None:
        if service_id is None or agent_id is None:
            raise TypeError(
                "tenant scope requires both service_id and agent_id when either is passed"
            )
        sid = str(service_id).strip()
        aid = str(agent_id).strip()
        if not sid or not aid:
            raise TypeError("tenant service_id/agent_id must be non-empty strings")
        return sid, aid
    bound = get_bound_agent_env_ns()
    if bound is not None:
        return bound
    raise TypeError(
        "tenant scope is required: pass service_id=... and agent_id=..., "
        "or bind_agent_env_ns before resolving tenant paths"
    )


def resolve_tenant_agent_workspace_dir(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> Path:
    """Resolve ``service_{sid}/agent_{aid}/agent/jiuwenclaw_workspace``."""
    sid, aid = resolve_tenant_env_ns(service_id, agent_id)
    workspace = get_multi_tenant_user_workspace_dir(sid, aid)
    if workspace is None:
        raise TypeError(
            f"invalid tenant for workspace path: service_id={sid!r}, agent_id={aid!r}"
        )
    return workspace / "agent" / "jiuwenclaw_workspace"


def resolve_tenant_agent_root_dir(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> Path:
    """Resolve ``service_{sid}/agent_{aid}/agent``."""
    sid, aid = resolve_tenant_env_ns(service_id, agent_id)
    workspace = get_multi_tenant_user_workspace_dir(sid, aid)
    if workspace is None:
        raise TypeError(
            f"invalid tenant for agent root: service_id={sid!r}, agent_id={aid!r}"
        )
    return workspace / "agent"


def resolve_gateway_cron_jobs_path(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> Path:
    """Gateway per-tenant cron store: ``gateway/cron/service_{sid}/agent_{aid}/cron_jobs.json``."""
    sid = str(service_id or "default").strip() or "default"
    aid = str(agent_id or "default").strip() or "default"
    return (
        get_user_workspace_dir()
        / "gateway"
        / "cron"
        / f"service_{sid}"
        / f"agent_{aid}"
        / "cron_jobs.json"
    )


def resolve_cron_tenant_scope(
    *,
    service_id: str | None = None,
    agent_id: str | None = None,
    metadata: dict[str, Any] | None = None,
    params: dict[str, Any] | None = None,
    log_prefix: str = "[Cron]",
) -> tuple[str, str]:
    """Resolve cron tenant ids; missing values fall back to default/default (5b)."""
    _logger = logging.getLogger(__name__)
    sid = service_id
    aid = agent_id
    if sid is None and isinstance(metadata, dict):
        sid = metadata.get("service_id")
    if aid is None and isinstance(metadata, dict):
        aid = metadata.get("agent_id")
    if sid is None and isinstance(params, dict):
        sid = params.get("service_id")
    if aid is None and isinstance(params, dict):
        aid = params.get("agent_id")
    sid_s = str(sid).strip() if sid is not None else ""
    aid_s = str(aid).strip() if aid is not None else ""
    if not sid_s or not aid_s:
        _logger.warning(
            "%s missing service_id/agent_id; fallback to default/default (sid=%r aid=%r)",
            log_prefix,
            sid,
            aid,
        )
    return sid_s or "default", aid_s or "default"


def resolve_tenant_env_ns_from_agent(agent: Any) -> tuple[str, str] | None:
    """Read tenant ids from a DeepAgent/adapter when env_* attrs are present."""
    if agent is None:
        return None
    sid = getattr(agent, "_env_service_id", None) or getattr(agent, "_service_id", None)
    aid = getattr(agent, "_env_agent_id", None) or getattr(agent, "_agent_id", None)
    if sid is None or aid is None:
        return None
    sid_s = str(sid).strip()
    aid_s = str(aid).strip()
    if not sid_s or not aid_s:
        return None
    return sid_s, aid_s


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
      ``[<multi-tenant user workspace>/agent/jiuwenclaw_workspace/skills]``.
    - Single-tenant (both ``None``): returns ``[get_agent_skills_dir()]``.
    """
    if service_id or agent_id:
        workspace = get_multi_tenant_user_workspace_dir(service_id, agent_id)
        if workspace is not None:
            return [workspace / "agent" / "jiuwenclaw_workspace" / "skills"]
    return [get_agent_skills_dir()]


JIUWENCLAW_SHARED_SKILLS_DIRS_ENV = "JIUWENCLAW_SHARED_SKILLS_DIRS"


def parse_shared_skills_dirs_raw(raw: str) -> list[Path]:
    """Parse JIUWENCLAW_SHARED_SKILLS_DIRS value into deduplicated absolute paths."""
    text = (raw or "").strip()
    if not text:
        return []

    dirs: list[Path] = []
    seen: set[str] = set()
    for part in [part.strip() for part in text.split(os.pathsep) if part.strip()]:
        path = Path(part).expanduser().resolve()
        key = str(path)
        if key in seen:
            continue
        seen.add(key)
        dirs.append(path)
    return dirs


def get_shared_agent_skills_dirs() -> list[Path]:
    from jiuwenclaw.local_env_config import read_env

    raw = read_env("JIUWENCLAW_SHARED_SKILLS_DIRS", "")
    return parse_shared_skills_dirs_raw(raw)


_BOOTSTRAP_SKILL_ROW_RE = re.compile(r"^\|\s*`([a-z][-a-z0-9]*)`\s*\|", re.MULTILINE)


def parse_bootstrap_skill_names(content: str) -> set[str]:
    """Parse official skill names from ``BOOTSTRAP.md`` table rows.

    Matches relay-claw ``getBootstrapSkillNames``:
    ``| `skill-name` | ...``
    """
    names: set[str] = set()
    for match in _BOOTSTRAP_SKILL_ROW_RE.finditer(content or ""):
        name = match.group(1)
        if name:
            names.add(name)
    return names


_bootstrap_skill_names_cache: set[str] | None = None
_bootstrap_skill_names_cache_lock = threading.Lock()
_extra_bootstrap_skill_roots: set[str] = set()
_extra_bootstrap_skill_roots_lock = threading.Lock()


def prime_bootstrap_skill_roots(skill_dirs: str | list[str]) -> None:
    """Register skill roots from SkillEvolutionRail -- may differ from read_env timing."""
    global _bootstrap_skill_names_cache
    raw_dirs = [skill_dirs] if isinstance(skill_dirs, (str, Path)) else list(skill_dirs)
    with _extra_bootstrap_skill_roots_lock:
        for part in raw_dirs:
            _extra_bootstrap_skill_roots.add(str(Path(part).expanduser().resolve()))
    _bootstrap_skill_names_cache = None


def _iter_bootstrap_skill_roots() -> list[Path]:
    """Skill roots that may host ``BOOTSTRAP.md`` -- shared env + registered + rail dirs."""
    shared = get_shared_agent_skills_dirs()
    registered = resolve_agent_registered_skill_dirs()
    with _extra_bootstrap_skill_roots_lock:
        extra_keys = set(_extra_bootstrap_skill_roots)

    roots: list[Path] = []
    seen: set[str] = set()
    for path in (*shared, *registered, *(Path(key) for key in extra_keys)):
        key = str(path.resolve())
        if key in seen:
            continue
        seen.add(key)
        roots.append(path)

    return roots


def get_bootstrap_skill_names(*, refresh: bool = False) -> set[str]:
    """Return official skill names declared in shared ``BOOTSTRAP.md`` files."""
    global _bootstrap_skill_names_cache
    if not refresh:
        cached: None | set[str] = _bootstrap_skill_names_cache
        if cached is not None:
            return set(cached)

    with _bootstrap_skill_names_cache_lock:
        if not refresh and _bootstrap_skill_names_cache is not None:
            return set(_bootstrap_skill_names_cache)

        names: set[str] = set()
        roots = _iter_bootstrap_skill_roots()
        for skills_dir in roots:
            bootstrap_path = skills_dir / "BOOTSTRAP.md"
            if not bootstrap_path.is_file():
                continue
            try:
                content = bootstrap_path.read_text(encoding="utf-8")
            except OSError as exc:
                logger.debug("[bootstrap-skills] read failed -- path=%s (%s)", bootstrap_path, exc)
                continue
            names.update(parse_bootstrap_skill_names(content))

        # Avoid poisoning cache with {} before SHARED_SKILLS / session dirs are ready.
        if names or not roots:
            _bootstrap_skill_names_cache = set(names)
            if names:
                logger.info(
                    "[bootstrap-skills] builtin skills excluded from evolution -- %s",
                    sorted(names),
                )
            elif roots:
                logger.warning(
                    "[bootstrap-skills] no builtin skills parsed -- BOOTSTRAP.md missing or empty, roots=%s",
                    [str(p) for p in roots],
                )
        else:
            logger.warning(
                "[bootstrap-skills] no skill roots yet -- builtin filter deferred, not caching empty",
            )
        return set(names)


def is_bootstrap_builtin_skill(skill_name: str) -> bool:
    """Whether *skill_name* is an official preset skill from ``BOOTSTRAP.md``."""
    normalized = (skill_name or "").strip().lower()
    if not normalized:
        return False
    return normalized in get_bootstrap_skill_names()


def resolve_agent_registered_skill_dirs() -> list[Path]:
    """Resolve skill dirs: chat-bound adapter snapshot > shared env > default workspace skills."""
    from jiuwenclaw.agentserver.session_skill_dirs import (
        get_bound_session_registered_skill_dirs,
    )

    bound = get_bound_session_registered_skill_dirs()
    if bound is not None:
        return [Path(p) for p in bound]

    shared = get_shared_agent_skills_dirs()
    if shared:
        return shared
    return [get_agent_skills_dir()]


def get_agent_registered_skill_dirs() -> list[Path]:
    return resolve_agent_registered_skill_dirs()


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
    """Get the sessions directory path.

    Prefer request-bound tenant agent root when present; otherwise the default
    multi-tenant path (single-tenant as the default special case):
    ~/.jiuwenclaw/service_default/agent_default/agent/sessions
    """
    try:
        from jiuwenclaw.agentserver.tenant_context import get_bound_agent_root

        bound = get_bound_agent_root()
        if bound is not None:
            return bound / "sessions"
    except ImportError:
        logger.debug("tenant_context unavailable for sessions bind", exc_info=True)
    return get_multi_tenant_user_workspace_dir("default", "default") / "agent" / "sessions"


def resolve_tenant_sessions_dir(
    service_id: str | None,
    agent_id: str | None,
) -> Path:
    """Resolve ``service_{sid}/agent_{aid}/agent/sessions`` for a tenant pair.

    Falls back to :func:`get_agent_sessions_dir` when the pair is incomplete.
    """
    workspace = get_multi_tenant_user_workspace_dir(service_id, agent_id)
    if workspace is not None:
        return workspace / "agent" / "sessions"
    return get_agent_sessions_dir()


def normalize_tenant_scope_id(value: str | None, *, default: str = "default") -> str:
    """Normalize a tenant ``service_id`` or ``agent_id`` segment."""
    if value is None:
        return default
    stripped = str(value).strip()
    return stripped or default


def get_agent_evolution_trajectories_dir(
    service_id: str | None = None,
    agent_id: str | None = None,
) -> Path:
    """Get evolution trajectories directory under the tenant agent root.

    Path: ``service_{sid}/agent_{aid}/agent/evolution_trajectories``

    When ``service_id``/``agent_id`` are omitted, uses ``get_agent_root_dir()``
    (bound agent root, else ``service_default/agent_default/agent``).
    """
    if service_id is not None or agent_id is not None:
        return resolve_tenant_agent_root_dir(service_id, agent_id) / "evolution_trajectories"
    return get_agent_root_dir() / "evolution_trajectories"


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
    agent_default_path = get_multi_tenant_user_workspace_dir("default", "default")
    agent_root = get_agent_root_dir()

    # 确保 service 级目录存在（.logs 等）
    service_root.mkdir(parents=True, exist_ok=True)

    # 迁移 .checkpoint 到 agent_default 级别
    checkpoint_target = (
        agent_default_path / ".checkpoint" if agent_default_path else agent_root / ".checkpoint"
    )
    checkpoint_sources = [
        workspace / ".checkpoint",
        workspace / "agent" / ".checkpoint",
        agent_root / ".checkpoint",  # 从 agent 子目录迁移到 agent_default 级别
    ]
    logs_sources = [
        workspace / ".logs",
        workspace / "agent" / ".logs",
    ]
    legacy_sources_exist = any(p.exists() for p in checkpoint_sources + logs_sources)

    # agent_default 已存在（迁移归档）或确有 legacy 源待迁 → 才 mkdir / 搬文件
    if agent_default_path and (agent_default_path.exists() or legacy_sources_exist):
        if not agent_default_path.exists():
            agent_default_path.mkdir(parents=True, exist_ok=True)
        for legacy in checkpoint_sources:
            if legacy.exists() and not checkpoint_target.exists():
                checkpoint_target.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(legacy), str(checkpoint_target))
            elif legacy.exists() and checkpoint_target.exists():
                for f in legacy.iterdir():
                    if not (checkpoint_target / f.name).exists():
                        shutil.move(str(f), str(checkpoint_target / f.name))

        # 迁移 .logs 到 service 级别
        logs_target = service_root / ".logs"
        for legacy in logs_sources:
            if legacy.exists() and not logs_target.exists():
                shutil.move(str(legacy), str(logs_target))
            elif legacy.exists() and logs_target.exists():
                for f in legacy.iterdir():
                    if not (logs_target / f.name).exists():
                        shutil.move(str(f), str(logs_target / f.name))

    if agent_default_path and (agent_default_path.exists() or legacy_sources_exist):
        agent_root.mkdir(parents=True, exist_ok=True)


def get_checkpoint_dir() -> Path:
    """Get the checkpoint directory path (agent_id level).

    多租户架构下，checkpoint 存放在 agent_id 级别。
    Path: ~/.jiuwenclaw/service_default/agent_default/.checkpoint
    """
    _migrate_legacy_checkpoint_and_logs()
    workspace = get_multi_tenant_user_workspace_dir("default", "default")
    if workspace:
        return workspace / ".checkpoint"
    # Fallback
    return get_agent_root_dir() / ".checkpoint"


def _resolve_logs_service_id(service_id: str | None = None) -> str:
    """Resolve service_id for logs: explicit > bound env_ns > default."""
    if service_id is not None:
        return normalize_tenant_scope_id(service_id)
    try:
        from jiuwenclaw.local_env_config import get_bound_agent_env_ns

        ns = get_bound_agent_env_ns()
        if ns is not None:
            return normalize_tenant_scope_id(ns[0])
    except Exception:
        logger.debug("resolve logs service_id from bound env_ns failed", exc_info=True)
    return "default"


def get_logs_dir(service_id: str | None = None) -> Path:
    """Get the logs directory path (service-level).

    多租户架构下，日志存放在 service 级别，便于同 service 下多 agent 共享。

    Path: ``~/.jiuwenclaw/service_{sid}/.logs``

    ``service_id`` 解析顺序：显式参数 > 当前 ``bind_agent_env_ns`` 的 sid > ``default``。
    进程启动时的 FileHandler（``setup_logger``）通常无 bind，仍落在 ``service_default/.logs``。
    """
    _migrate_legacy_checkpoint_and_logs()
    sid = _resolve_logs_service_id(service_id)
    return get_service_root_dir(sid) / ".logs"


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


# 统一敏感信息掩码值。
_SENSITIVE_MASK = "******"
# 匹配常见敏感字段键值对（不要求值必须带引号），用于覆盖:
# - token=abc
# - api_key: sk-xxx
# - authorization = Bearer ...
# 分组说明：
# 1) 敏感键名；2) 分隔符及两侧空白（: 或 =）；3/4) 可选引号（当前替换逻辑未直接使用）
_KV_SENSITIVE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(password|passwd|pwd|secret|token|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|authorization|user[_-]?id|userid)"
    r"(?![A-Za-z0-9])(\s*[:=]\s*)([\"']?)[^,\s\"'\]\}]+([\"']?)"
)
# 匹配“键名包含敏感关键词”且“值被引号包裹”的场景，覆盖:
# - 'CAT_CAFE_CALLBACK_TOKEN': 'xxxx'
# - 'CAT_CAFE_USER_ID': 'CSDN-weixin'
# - "my_private_key"="xxxx"
# 分组说明：
# 1) 完整的 key + 分隔符（含可选引号）
# 2) 值的起始引号（' 或 "）
# 3) 值内容（非贪婪）
# 4) 结束引号（通过 (\2) 强制与起始引号一致）
_NAMED_SENSITIVE_KV_PATTERN = re.compile(
    r"(?i)([\"']?[A-Za-z0-9_.-]*"
    r"(?:token|secret|password|passwd|pwd|api[_-]?key|authorization|"
    r"credential|private[_-]?key|user[_-]?id|userid)"
    r"[A-Za-z0-9_.-]*[\"']?\s*[:=]\s*)([\"'])(.*?)(\2)"
)
# 匹配 Authorization Bearer 令牌，保留 "Bearer " 前缀，仅掩码后面的令牌值。
_BEARER_SENSITIVE_PATTERN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9\-._~+/]+=*")
_SENSITIVE_PATTERNS: list[re.Pattern[str]] = [
    # 匹配 JWT（header.payload.signature 三段式，常见以 eyJ 开头）。
    re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b"),
    # 匹配 OpenAI 风格 key（sk- 前缀）。
    re.compile(r"\bsk-[A-Za-z0-9]{8,}\b"),
    # 匹配 GitHub Personal Access Token（ghp_ 前缀）。
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    # 匹配 GitLab Personal Access Token（glpat- 前缀）。
    re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    # 匹配邮箱地址（避免日志中泄露个人身份信息）。
    re.compile(r"\b[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[A-Za-z]{2,}\b"),
    # 匹配中国大陆手机号（可带 +86 或 86 前缀，支持空格/短横线分隔）。
    re.compile(r"(?<!\d)(?:\+?86[-\s]?)?1[3-9]\d{9}(?!\d)"),
    # 匹配中国身份证号（18 位，最后一位可为 X/x）。
    re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"),
]


def _sanitize_log_text(text: str) -> str:
    if not text:
        return text

    masked = text
    masked = _KV_SENSITIVE_PATTERN.sub(r"\1\2" f"{_SENSITIVE_MASK}", masked)
    masked = _NAMED_SENSITIVE_KV_PATTERN.sub(r"\1\2" f"{_SENSITIVE_MASK}" r"\2", masked)
    masked = _BEARER_SENSITIVE_PATTERN.sub(r"\1" f"{_SENSITIVE_MASK}", masked)
    for pattern in _SENSITIVE_PATTERNS:
        masked = pattern.sub(_SENSITIVE_MASK, masked)
    return masked


class SensitiveDataFilter(logging.Filter):
    """Mask sensitive data in all log messages."""

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            message = record.getMessage()
            record.msg = _sanitize_log_text(message)
            record.args = ()
        except Exception:
            # Never block logging because of desensitization failure.
            pass
        return True


class JsonOnlyFormatter(logging.Formatter):
    """只输出message内容，不添加任何前缀（时间戳、级别、logger名）"""

    def format(self, record: logging.LogRecord) -> str:
        return record.getMessage()


class LoggingTagConfig:
    """用户可见性 Tag 配置管理

    管理两个配置项：
    - user_visible: 是否启用 [USER] Tag（默认 True）
    - user_progress_visible: 是否启用 [USER_PROGRESS] Tag（默认 True）

    配置优先级：环境变量 > config.yaml > 默认值
    """

    user_visible: bool = True
    user_progress_visible: bool = True
    _config_file_path: Optional[Path] = None
    _env_prefix: str = "JIUWENCLAW_LOG_"
    _skip_env_load: bool = False

    def __init__(self, skip_env_load: bool = False):
        """初始化配置并加载配置

        Args:
            skip_env_load: 是否跳过配置加载（用于测试）
        """
        self._skip_env_load = skip_env_load
        self.__post_init__()

    def __post_init__(self):
        """初始化后加载配置"""
        # 如果 skip_env_load 为 True（用于测试），跳过配置加载
        if self._skip_env_load:
            self.user_visible = True
            self.user_progress_visible = True
            return
        self._load_config()

    def _load_config(self):
        """加载配置（环境变量 + config.yaml）"""
        # 1. 默认值
        base_user_visible = True
        base_user_progress_visible = True

        # 2. 从环境变量加载（优先级最高）
        user_visible = self._load_from_env("USER_VISIBLE", base_user_visible)
        user_progress_visible = self._load_from_env("USER_PROGRESS_VISIBLE", base_user_progress_visible)

        # 3. 如果环境变量未设置(None)，从 config.yaml 加载
        env_user_visible = os.getenv(f"{self._env_prefix}USER_VISIBLE")
        env_user_progress_visible = os.getenv(f"{self._env_prefix}USER_PROGRESS_VISIBLE")

        if env_user_visible is None:
            user_visible = self._load_from_yaml("user_visible", user_visible)
        if env_user_progress_visible is None:
            user_progress_visible = self._load_from_yaml("user_progress_visible", user_progress_visible)

        self.user_visible = user_visible
        self.user_progress_visible = user_progress_visible

    def _load_from_env(self, key: str, default: bool) -> bool:
        """从环境变量加载配置

        Args:
            key: 配置键名（如 "USER_VISIBLE"）
            default: 默认值

        Returns:
            bool: 配置值，如果环境变量未设置或无效则返回默认值
        """
        env_key = f"{self._env_prefix}{key}"
        env_value = os.getenv(env_key)

        if env_value is None:
            return default

        # 解析布尔值（支持多种格式）
        value = env_value.strip().lower()
        if value in ('true', '1', 'yes', 'on'):
            return True
        elif value in ('false', '0', 'no', 'off'):
            return False
        else:
            logger.warning(
                f"无效的环境变量 '{env_key}': '{env_value}', "
                f"期望 'true'/'false'（或 '1'/'0'、'yes'/'no'、'on'/'off'），使用默认值 '{default}'"
            )
            return default

    @classmethod
    def _load_from_yaml(cls, key: str, default: bool) -> bool:
        """从 config.yaml 加载配置

        Args:
            key: 配置键名（如 "user_visible"）
            default: 默认值

        Returns:
            bool: 配置值，如果配置文件不存在或该键未设置则返回默认值
        """
        try:
            config_data = _load_logging_config_from_yaml()
            if not config_data:
                return default

            value = config_data.get(key)
            if value is None:
                return default

            if isinstance(value, bool):
                return value
            else:
                logger.warning(
                    f"config.yaml 中的 logging.{key} 不是布尔值: '{value}', 使用默认值 '{default}'"
                )
                return default
        except Exception as e:
            logger.warning(f"加载 config.yaml 中的 logging.{key} 失败: {e}, 使用默认值 '{default}'")
            return default

    def is_user_visible_enabled(self) -> bool:
        """检查 [USER] Tag 是否启用

        Returns:
            bool: True 表示启用，False 表示禁用
        """
        return self.user_visible

    def is_user_progress_visible_enabled(self) -> bool:
        """检查 [USER_PROGRESS] Tag 是否启用

        Returns:
            bool: True 表示启用，False 表示禁用
        """
        return self.user_progress_visible


class JsonUserVisibleFormatter(jsonlogger.JsonFormatter if jsonlogger else logging.Formatter):
    """JSON 格式化日志输出

    继承 pythonjsonlogger.JsonFormatter，扩展以下特性：
    1. 时间戳格式与文本格式一致："2026-05-07 11:33:22.537"
    2. user_visible 字段："critical"/"progress"/null
    3. component 字段：自动推导组件分类
    4. 敏感数据脱敏：自动应用 _sanitize_log_text
    5. 异常信息简化：仅包含类型和消息
    6. 中文编码支持：json_ensure_ascii=False

    使用方式：
        logger.info("消息", extra={'user_visible': 'critical'})
    """

    # 标准字段名映射（pythonjsonlogger 默认字段名 -> 我们的字段名）
    _FIELD_RENAME_MAP = {
        'asctime': 'timestamp',
        'levelname': 'level',
        'name': 'logger',
    }

    def __init__(
        self,
        fmt: Optional[str] = None,
        datefmt: Optional[str] = None,
        style: str = '%',
        validate: bool = True,
        timestamp_format: str = 'text',
        include_component: bool = True,
        sanitize_sensitive_data: bool = True,
        exc_info_style: str = 'simple',
        *args,
        **kwargs
    ):
        """初始化 JSON 格式化器

        Args:
            timestamp_format: 时间戳格式
                - 'text': 与文本格式一致 "2026-05-07 11:33:22.537"（默认）
                - 'iso8601': ISO 8601 格式 "2026-05-07T11:33:22.537Z"（可选）
            include_component: 是否包含组件分类字段（默认 True）
            sanitize_sensitive_data: 是否启用敏感数据脱敏（默认 True）
            exc_info_style: 异常信息风格
                - 'simple': 仅包含异常类型和消息（默认）
                - 'full': 包含完整堆栈信息（可选）
        """
        if jsonlogger is None:
            # 如果 pythonjsonlogger 未安装，fallback 到标准 Formatter
            if fmt is None:
                fmt = "%(asctime)s %(levelname)s %(name)s %(message)s"
            if datefmt is None:
                datefmt = "%Y-%m-%d %H:%M:%S"
            super().__init__(fmt, datefmt, style, validate, *args, **kwargs)
        else:
            # pythonjsonlogger 默认格式
            if fmt is None:
                fmt = '%(asctime)s %(levelname)s %(name)s %(message)s'
            if datefmt is None:
                datefmt = "%Y-%m-%d %H:%M:%S"
            # 设置 json_ensure_ascii=False（保持中文可读，不转义为 \uXXXX）
            kwargs['json_ensure_ascii'] = False
            super().__init__(fmt, datefmt, style, validate, *args, **kwargs)

        self.timestamp_format = timestamp_format
        self.include_component = include_component
        self.sanitize_sensitive_data = sanitize_sensitive_data
        self.exc_info_style = exc_info_style

    def add_fields(
        self,
        log_record: Dict[str, Any],
        record: logging.LogRecord,
        message_dict: Dict[str, Any]
    ) -> None:
        """添加 JSON 字段

        Args:
            log_record: JSON 输出字典（会被序列化）
            record: Python logging LogRecord
            message_dict: 消息字典
        """
        # 如果 pythonjsonlogger 未安装，直接返回
        if jsonlogger is None:
            return

        # 调用父类方法添加基础字段
        super().add_fields(log_record, record, message_dict)

        # 立即删除user_visible字段（无论值是什么），后续会重新验证并添加有效值
        # merge_record_extra会自动添加所有record属性，我们只保留有效的user_visible值
        if 'user_visible' in log_record:
            del log_record['user_visible']

        # 字段重命名
        for old_key, new_key in self._FIELD_RENAME_MAP.items():
            if old_key in log_record:
                log_record[new_key] = log_record.pop(old_key)

        # 处理时间戳格式
        if 'timestamp' in log_record and self.timestamp_format == 'text':
            # 保持与文本格式一致："2026-05-07 11:33:22.537"
            timestamp = log_record['timestamp']
            if isinstance(timestamp, str):
                # pythonjsonlogger 默认输出 ISO 8601 格式，转换为文本格式
                try:
                    dt = datetime.fromisoformat(timestamp.replace('Z', '+00:00'))
                    log_record['timestamp'] = dt.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3]
                except Exception:
                    pass  # 保持原格式

        # 添加进程ID和lineno字段（始终添加）
        # 创建有序字典以保证字段顺序：timestamp → process → level → user_tag → logger → lineno → message → 其他字段
        ordered_record = OrderedDict()

        # 按顺序添加字段
        if 'timestamp' in log_record:
            ordered_record['timestamp'] = log_record['timestamp']

        ordered_record['process'] = record.process

        if 'level' in log_record:
            ordered_record['level'] = log_record['level']

        # 处理 user_tag 字段（从record的user_tag属性添加到JSON输出）
        # user_tag需要在logger之前
        user_tag = getattr(record, 'user_tag', None)
        if user_tag is not None and user_tag != '':
            # user_tag 是 "[USER] " 或 "[USER_PROGRESS] "
            ordered_record['user_tag'] = user_tag
        # 当 user_tag 为空字符串时，不添加该字段（保持JSON清洁）

        # 添加身份字段（user_id、domain_id、app_id）
        # 身份字段在 user_tag 之后，logger 之前
        # 始终输出身份字段（null 值便于日志聚合分析）
        user_id = getattr(record, 'user_id', None)
        domain_id = getattr(record, 'domain_id', None)
        app_id = getattr(record, 'app_id', None)
        ordered_record['user_id'] = user_id
        ordered_record['domain_id'] = domain_id
        ordered_record['app_id'] = app_id

        if 'logger' in log_record:
            ordered_record['logger'] = log_record['logger']

        # lineno字段在logger之后
        ordered_record['lineno'] = record.lineno

        if 'message' in log_record:
            ordered_record['message'] = log_record['message']

        # 添加其他字段（exc_info、component等）
        identity_keys = {'user_id', 'domain_id', 'app_id'}
        for key, value in log_record.items():
            if key not in ordered_record:
                if key in identity_keys and value is None:
                    continue
                ordered_record[key] = value

        # 替换原log_record为有序字典
        log_record.clear()
        log_record.update(ordered_record)

        # 添加组件分类字段
        if self.include_component:
            log_record['component'] = _log_component_from_logger_name(record.name)

        # 处理 user_visible 字段
        user_visible = getattr(record, 'user_visible', None)
        if user_visible is not None:
            # 验证 user_visible 值有效性
            if user_visible not in ('critical', 'progress'):
                logger.warning(
                    f"无效的 user_visible 值: '{user_visible}'，期望 'critical' 或 'progress'"
                )
                user_visible = None

            # 只在 user_visible 有有效值时才添加到 JSON 输出中
            # 这样可以避免输出 "user_visible": null，保持 JSON 清洁
            if user_visible is not None:
                log_record['user_visible'] = user_visible
        # 当 user_visible 为 None 时，不添加该字段到 JSON 输出中

        # 敏感数据脱敏
        if self.sanitize_sensitive_data and 'message' in log_record:
            log_record['message'] = _sanitize_log_text(log_record['message'])

        # 异常信息简化
        if 'exc_info' in log_record and log_record['exc_info']:
            if self.exc_info_style == 'simple':
                # 简化为类型和消息
                exc_info = log_record['exc_info']
                if isinstance(exc_info, tuple) and len(exc_info) >= 2:
                    exc_type, exc_value = exc_info[:2]
                    log_record['exc_info'] = f"{exc_type.__name__}: {exc_value}"
            # 'full' 模式保持原样


class UserVisibleTagFilter(logging.Filter):
    """用户可见性 Tag 过滤器

    根据日志记录的 user_visible 属性添加对应的 Tag：
    - [USER]: 关键用户操作（user_visible='critical'）
    - [USER_PROGRESS]: 进度信息（user_visible='progress'）

    使用方式：
        logger.info("消息", extra={'user_visible': 'critical'})
    """

    _USER_TAG = "[USER]"
    _USER_PROGRESS_TAG = "[USER_PROGRESS]"
    _USER_VISIBLE_ATTR = "user_visible"
    _TAG_VALUE_CRITICAL = 'critical'
    _TAG_VALUE_PROGRESS = 'progress'

    def __init__(self, tag_config: Optional[LoggingTagConfig] = None):
        """初始化 Tag 过滤器

        Args:
            tag_config: Tag 配置对象（可选，默认创建新实例）
        """
        super().__init__()
        self.tag_config = tag_config if tag_config is not None else LoggingTagConfig()

    def filter(self, record: logging.LogRecord) -> bool:
        """根据 user_visible 属性添加 Tag

        Args:
            record: 日志记录对象

        Returns:
            bool: 总是返回 True（不过滤日志，仅添加 Tag）
        """
        # 获取 user_visible 属性
        user_visible = getattr(record, self._USER_VISIBLE_ATTR, None)

        # 设置自定义字段而不是修改消息（修复重复标签和位置问题）
        # 默认设置为空字符串，确保formatter总能找到这个字段
        if user_visible == self._TAG_VALUE_CRITICAL and self.tag_config.is_user_visible_enabled():
            record.user_tag = "[USER] "
        elif user_visible == self._TAG_VALUE_PROGRESS and self.tag_config.is_user_progress_visible_enabled():
            record.user_tag = "[USER_PROGRESS] "
        else:
            # 无标记、未知值、或配置禁用时设置为空字符串
            record.user_tag = ""

        return True


class IdentityFieldFilter(logging.Filter):
    """身份信息字段过滤器。

    自动为每条日志添加 user_id、domain_id、app_id 字段。
    从 IdentityStore 单例读取当前身份信息。

    使用方式：
        自动应用于所有 Handler，无需手动调用。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        """添加身份字段到 LogRecord。

        Args:
            record: 日志记录对象。

        Returns:
            bool: 总是返回 True（不过滤日志，仅添加字段）。
        """
        from jiuwenclaw.extensions.identity_provider import IdentityStore

        identity = IdentityStore.get_instance().get_identity()
        if identity is not None:
            record.user_id = identity.user_id
            record.domain_id = identity.domain_id
            record.app_id = identity.app_id
        else:
            record.user_id = None
            record.domain_id = None
            record.app_id = None
        return True


class IdentityTextFormatter(logging.Formatter):
    """支持身份字段的文本格式 Formatter。

    在文本日志中添加身份字段（user_id、domain_id、app_id），
    仅在有值时输出，格式为：user_id=xxx domain_id=xxx app_id=xxx
    """

    def format(self, record: logging.LogRecord) -> str:
        """格式化日志记录，添加身份字段。

        Args:
            record: 日志记录对象。

        Returns:
            str: 格式化后的日志字符串。
        """
        # 构建身份字段字符串（始终输出，便于日志聚合分析）
        identity_parts = []
        user_id = getattr(record, 'user_id', None)
        domain_id = getattr(record, 'domain_id', None)
        app_id = getattr(record, 'app_id', None)

        # 使用 "null" 表示空值，保持格式一致性
        identity_parts.append(f"user_id={user_id if user_id is not None else 'null'}")
        identity_parts.append(f"domain_id={domain_id if domain_id is not None else 'null'}")
        identity_parts.append(f"app_id={app_id if app_id is not None else 'null'}")

        # 将身份字段字符串添加到 record，供格式化使用
        record.identity = " " + " ".join(identity_parts) + " "

        return super().format(record)


def _resolve_json_config() -> Dict[str, Any]:
    """解析JSON格式化子配置段

    从 config.yaml 读取 logging.json 子段，提供默认值和验证。

    Returns:
        Dict[str, Any]: json配置字典，包含：
            - timestamp_format: 'text' | 'iso8601'
            - include_component: bool
            - sanitize_sensitive_data: bool
            - exc_info_style: 'simple' | 'full'
    """
    # 默认配置
    default_config = {
        'timestamp_format': 'text',
        'include_component': True,
        'sanitize_sensitive_data': True,
        'exc_info_style': 'simple'
    }

    # 从config.yaml读取
    config_data = _load_logging_config_from_yaml()
    if not config_data:
        return default_config

    json_config = config_data.get('json', {})
    if not isinstance(json_config, dict):
        return default_config

    # 合并默认值
    result = default_config.copy()
    result.update(json_config)

    # 验证字段值
    result = _validate_json_config(result)

    return result


def _validate_json_config(config: Dict[str, Any]) -> Dict[str, Any]:
    """验证JSON配置字段值有效性

    Args:
        config: json配置字典

    Returns:
        Dict[str, Any]: 验证后的配置字典（无效值使用默认值）
    """
    # 验证 timestamp_format
    if config.get('timestamp_format') not in ('text', 'iso8601'):
        logger.warning(
            f"无效的logging.json.timestamp_format配置: '{config.get('timestamp_format')}', "
            f"期望 'text' 或 'iso8601'，使用默认值 'text'"
        )
        config['timestamp_format'] = 'text'

    # 验证 include_component
    if 'include_component' in config and not isinstance(config['include_component'], bool):
        logger.warning(
            f"无效的logging.json.include_component配置: '{config.get('include_component')}', "
            f"期望布尔值，使用默认值 True"
        )
        config['include_component'] = True

    # 验证 sanitize_sensitive_data
    if 'sanitize_sensitive_data' in config and not isinstance(config['sanitize_sensitive_data'], bool):
        logger.warning(
            f"无效的logging.json.sanitize_sensitive_data配置: '{config.get('sanitize_sensitive_data')}', "
            f"期望布尔值，使用默认值 True"
        )
        config['sanitize_sensitive_data'] = True

    # 验证 exc_info_style
    exc_style = config.get('exc_info_style', 'simple')
    if exc_style not in ('simple', 'full'):
        logger.warning(
            f"无效的logging.json.exc_info_style配置: '{exc_style}', "
            f"期望 'simple' 或 'full'，使用默认值 'simple'"
        )
        config['exc_info_style'] = 'simple'

    return config


def _resolve_logging_format() -> str:
    """解析日志格式配置（text/json/dual）

    配置优先级：环境变量 > config.yaml > 默认值

    Returns:
        str: 'text' / 'json' / 'dual'
    """
    # 1. 从环境变量加载（最高优先级）
    env_format = os.getenv("JIUWENCLAW_LOG_FORMAT")
    if env_format:
        format_value = env_format.strip().lower()
        if format_value in ('text', 'json', 'dual'):
            return format_value
        else:
            logger.warning(
                f"无效的日志格式环境变量: '{env_format}', "
                f"期望 'text'、'json' 或 'dual', 使用默认值 'text'"
            )

    # 2. 从 config.yaml 加载
    try:
        config_data = _load_logging_config_from_yaml()
        if config_data:
            format_value = config_data.get('format')
            if format_value and format_value in ('text', 'json', 'dual'):
                return format_value

            # 3. 向后兼容：检测 dual_output.enabled=true 自动映射为 format=dual
            dual_output = config_data.get('dual_output')
            if isinstance(dual_output, dict):
                dual_enabled = dual_output.get('enabled')
                if dual_enabled is True:
                    logger.warning(
                        "检测到旧配置 dual_output.enabled=true，已自动映射为 format=dual"
                    )
                    return 'dual'
    except Exception as e:
        logger.warning(f"加载 config.yaml 中的 logging.format 失败: {e}")

    # 4. 默认值
    return 'text'


def _resolve_output_switches() -> Dict[str, bool]:
    """解析输出开关配置（console_enabled/file_enabled）

    配置优先级：环境变量 > config.yaml > 默认值

    Returns:
        Dict[str, bool]: {'console_enabled': bool, 'file_enabled': bool}
    """
    result = {'console_enabled': True, 'file_enabled': True}

    # 1. 控制台开关（console_enabled）
    env_console = os.getenv("JIUWENCLAW_LOG_CONSOLE_ENABLED")
    if env_console:
        value = env_console.strip().lower()
        if value in ('true', '1', 'yes', 'on'):
            result['console_enabled'] = True
        elif value in ('false', '0', 'no', 'off'):
            result['console_enabled'] = False
        else:
            logger.warning(
                f"无效的控制台开关环境变量: '{env_console}', "
                f"期望 'true'/'false'（或 '1'/'0'、'yes'/'no'、'on'/'off'），使用默认值 'true'"
            )
    else:
        # 从 config.yaml 加载
        try:
            config_data = _load_logging_config_from_yaml()
            if config_data:
                console_enabled = config_data.get('console_enabled')
                if isinstance(console_enabled, bool):
                    result['console_enabled'] = console_enabled
        except (FileNotFoundError, yaml.YAMLError, PermissionError) as e:
            # 具体的配置文件相关异常，记录日志后使用默认值
            logger.debug("无法从 config.yaml 加载 console_enabled 配置，使用默认值: %s", e)
        except Exception as e:
            # 其他未预期的异常，记录警告日志后使用默认值
            logger.warning("加载 console_enabled 配置时发生未预期错误: %s，使用默认值", e)

    # 2. 文件开关（file_enabled）
    env_file = os.getenv("JIUWENCLAW_LOG_FILE_ENABLED")
    if env_file:
        value = env_file.strip().lower()
        if value in ('true', '1', 'yes', 'on'):
            result['file_enabled'] = True
        elif value in ('false', '0', 'no', 'off'):
            result['file_enabled'] = False
        else:
            logger.warning(
                "无效的文件开关环境变量: '%s', "
                "期望 'true'/'false'（或 '1'/'0'、'yes'/'no'、'on'/'off'），使用默认值 'true'",
                env_file,
            )
    else:
        # 从 config.yaml 加载
        try:
            config_data = _load_logging_config_from_yaml()
            if config_data:
                file_enabled = config_data.get('file_enabled')
                if isinstance(file_enabled, bool):
                    result['file_enabled'] = file_enabled
        except (FileNotFoundError, yaml.YAMLError, PermissionError) as e:
            # 具体的配置文件相关异常，记录日志后使用默认值
            logger.debug("无法从 config.yaml 加载 file_enabled 配置，使用默认值: %s", e)
        except Exception as e:
            # 其他未预期的异常，记录警告日志后使用默认值
            logger.warning("加载 file_enabled 配置时发生未预期错误: %s，使用默认值", e)

    return result


_log_queue: _queue.SimpleQueue | None = None
_log_listener: QueueListener | None = None
# respect_handler_level 是 Python 3.12+ 才有的参数，模块级缓存避免每次 setup_logger 重复检查
_SUPPORTS_RESPECT_HANDLER_LEVEL: bool = sys.version_info >= (3, 12)


def setup_logger(log_level: Optional[str] = None) -> logging.Logger:
    """配置 ``jiuwenclaw`` 根日志：控制台 + 分组件文件 + 汇总 full.log。

    各模块应使用 ``logging.getLogger(__name__)``，分文件规则：
    - ``jiuwenclaw.channel.*`` → channel.log
    - ``jiuwenclaw.agents.*`` 或 ``jiuwenclaw.server.*`` → agent_server.log
    - 其余 ``jiuwenclaw.*``（含 ``jiuwenclaw.app``、gateway、evolution、utils 等）→ gateway.log

    所有分类日志同时写入 ``full.log``。输出目录：``~/.jiuwenclaw/agent/.logs/``。

    级别由 ``config.yaml`` 的 ``logging`` 段控制；环境变量 ``LOG_LEVEL`` 仅覆盖**控制台**级别
    （``log_level`` 参数为 ``None`` 时）。``JIUWENCLAW_LOG_LEVEL`` 或传入 ``log_level``
    会同时覆盖控制台与各文件级别，且显式参数优先（如单测）。

    扩展功能：
    - format 配置（text/json/dual）：通过 config.yaml 或 JIUWENCLAW_LOG_FORMAT 环境变量控制
    - console_enabled/file_enabled：控制输出开关
    - JSON 格式化：使用 JsonUserVisibleFormatter
    - user_visible Tag：使用 UserVisibleTagFilter（仅文本格式）
    """
    log_root_path = os.getenv("LOG_ROOT_PATH", "").strip()
    logs_root = Path(log_root_path).expanduser().resolve() if log_root_path else get_logs_dir()
    logs_root.mkdir(parents=True, exist_ok=True)

    levels = _resolve_logging_levels(log_level)

    # 解析日志格式配置（text/json/dual）
    log_format = _resolve_logging_format()

    # 解析输出开关配置
    output_switches = _resolve_output_switches()
    console_enabled = output_switches['console_enabled']
    file_enabled = output_switches['file_enabled']

    root = logging.getLogger("jiuwenclaw")
    root.setLevel(levels.logger)
    root.propagate = False
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    # 停止旧的 QueueListener（支持重复调用 setup_logger）
    global _log_queue, _log_listener
    if _log_listener is not None:
        _log_listener.stop()
        # 关闭 QueueListener 上的目标 handler（文件句柄等），避免 ResourceWarning
        for _old_h in _log_listener.handlers:
            _old_h.close()
        _log_listener = None
    _log_queue = _queue.SimpleQueue()
    _listener_targets: list[logging.Handler] = []

    # 解析JSON子配置段
    json_config = _resolve_json_config() if log_format in ('json', 'dual') else {}

    # 根据format选择Formatter
    # 文本格式字符串（进程ID和行号始终显示，身份字段在 user_tag 之后）
    text_fmt = (
        "%(asctime)s.%(msecs)03d [%(process)d] %(levelname)s "
        "%(identity)s%(user_tag)s%(name)s:%(lineno)d: %(message)s"
    )

    if log_format in ('json', 'dual'):
        # JSON 格式使用 JsonUserVisibleFormatter，动态读取配置参数
        json_formatter = JsonUserVisibleFormatter(
            timestamp_format=json_config.get('timestamp_format', 'text'),
            include_component=json_config.get('include_component', True),
            sanitize_sensitive_data=json_config.get('sanitize_sensitive_data', True),
            exc_info_style=json_config.get('exc_info_style', 'simple')
        )
        # 文本格式用于 dual 模式或作为 fallback
        text_formatter = IdentityTextFormatter(
            fmt=text_fmt,
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    else:
        # 文本格式使用 IdentityTextFormatter + UserVisibleTagFilter
        text_formatter = IdentityTextFormatter(
            fmt=text_fmt,
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        json_formatter = None

    privacy_filter = SensitiveDataFilter()

    # 初始化 LoggingTagConfig（仅文本格式需要）
    tag_config = LoggingTagConfig() if log_format in ('text', 'dual') else None

    def _add_rotating(
        filename: str,
        level: int,
        name_filter: Optional[_ComponentNameFilter] = None,
        custom_formatter: Optional[logging.Formatter] = None,
        use_json: bool = False,
    ) -> None:
        """添加文件 Handler

        Args:
            filename: 日志文件名
            level: 日志级别
            name_filter: 组件过滤器
            custom_formatter: 自定义格式化器
            use_json: 是否使用JSON格式（仅format=json/dual时有效）
        """
        if not file_enabled:
            return

        h = SafeRotatingFileHandler(
            filename=logs_root / filename,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        h.setLevel(level)

        # 选择formatter
        if custom_formatter is not None:
            h.setFormatter(custom_formatter)
        elif use_json and json_formatter is not None:
            h.setFormatter(json_formatter)
        else:
            h.setFormatter(text_formatter)

        h.addFilter(privacy_filter)

        # 为所有文件 handler 添加 IdentityFieldFilter（自动添加身份字段）
        h.addFilter(IdentityFieldFilter())

        # 为所有文件 handler 添加 UserVisibleTagFilter（仅在 text/dual 模式下）
        if tag_config:
            h.addFilter(UserVisibleTagFilter(tag_config))

        if name_filter is not None:
            h.addFilter(name_filter)
        _listener_targets.append(h)

    # 根据format配置输出文件策略
    if log_format == 'text':
        # text模式：仅输出.log文件（文本格式）
        _add_rotating("gateway.log", levels.gateway, _ComponentNameFilter("gateway"))
        _add_rotating("channel.log", levels.channel, _ComponentNameFilter("channel"))
        _add_rotating("agent_server.log", levels.agent_server,
            _CompositeFilter([_ComponentNameFilter("agent_server"), _ComponentNameFilter("permissions")]))
        _add_rotating("full.log", levels.full, None)
        permissions_formatter = JsonOnlyFormatter()
        _add_rotating("permissions.log", levels.agent_server, 
                      _ComponentNameFilter("permissions"), permissions_formatter)

    elif log_format == 'json':
        # json模式：仅输出.json文件（JSON格式）
        _add_rotating("gateway.json", levels.gateway, _ComponentNameFilter("gateway"), use_json=True)
        _add_rotating("channel.json", levels.channel, _ComponentNameFilter("channel"), use_json=True)
        _add_rotating("agent_server.json", levels.agent_server,
            _CompositeFilter([_ComponentNameFilter("agent_server"), 
                              _ComponentNameFilter("permissions")]), use_json=True)
        _add_rotating("full.json", levels.full, None, use_json=True)
        # permissions.log 保持特殊处理（使用JsonOnlyFormatter）
        permissions_formatter = JsonOnlyFormatter()
        _add_rotating("permissions.log", levels.agent_server, 
                      _ComponentNameFilter("permissions"), permissions_formatter)

    elif log_format == 'dual':
        # dual模式：同时输出.log和.json文件
        # .log文件（文本格式）
        _add_rotating("gateway.log", levels.gateway, _ComponentNameFilter("gateway"))
        _add_rotating("channel.log", levels.channel, _ComponentNameFilter("channel"))
        _add_rotating("agent_server.log", levels.agent_server,
            _CompositeFilter([_ComponentNameFilter("agent_server"), _ComponentNameFilter("permissions")]))
        _add_rotating("full.log", levels.full, None)
        permissions_formatter = JsonOnlyFormatter()
        _add_rotating("permissions.log", levels.agent_server, 
                      _ComponentNameFilter("permissions"), permissions_formatter)

        # .json文件（JSON格式）
        _add_rotating("gateway.json", levels.gateway, _ComponentNameFilter("gateway"), use_json=True)
        _add_rotating("channel.json", levels.channel, _ComponentNameFilter("channel"), use_json=True)
        _add_rotating("agent_server.json", levels.agent_server,
            _CompositeFilter([_ComponentNameFilter("agent_server"), 
                              _ComponentNameFilter("permissions")]), use_json=True)
        _add_rotating("full.json", levels.full, None, use_json=True)

    # 控制台输出（如果启用）
    if console_enabled:
        stream_handler = logging.StreamHandler()
        stream_handler.setLevel(levels.console)

        # 根据format选择控制台formatter
        if log_format == 'json':
            stream_handler.setFormatter(json_formatter)
        else:
            stream_handler.setFormatter(text_formatter)
            # 文本格式添加UserVisibleTagFilter
            if tag_config:
                stream_handler.addFilter(UserVisibleTagFilter(tag_config))

        # 控制台也添加 IdentityFieldFilter
        stream_handler.addFilter(IdentityFieldFilter())
        stream_handler.addFilter(privacy_filter)
        _listener_targets.append(stream_handler)

    # 用 QueueHandler 包装所有 handler，避免文件 I/O 和 _sanitize_log_text 阻塞事件循环
    if _listener_targets:
        _qh = QueueHandler(_log_queue)
        _qh.setLevel(logging.NOTSET)
        root.addHandler(_qh)
        if _SUPPORTS_RESPECT_HANDLER_LEVEL:
            # Python 3.12+：QueueListener 原生支持级别过滤
            _log_listener = QueueListener(_log_queue, *_listener_targets, respect_handler_level=True)
        else:
            # Python 3.11 fallback：给每个 handler 加 Filter 模拟级别过滤
            for _h in _listener_targets:
                _h.addFilter(lambda _r, _h=_h: _r.levelno >= _h.level)
            _log_listener = QueueListener(_log_queue, *_listener_targets)
        _log_listener.start()

    try:
        from jiuwenclaw.interface_resp import ensure_interface_logger

        ensure_interface_logger()
    except Exception:
        logger.debug("interface.log handler setup skipped", exc_info=True)

    return root


class AsyncLRUCache:
    """带可选过期时间与容量上限的 LRU 缓存（异步并发安全）.

    ``max_size=None`` / ``ttl_seconds=None`` 表示不启用对应限制。
    """

    def __init__(
        self,
        max_size: int | None = None,
        ttl_seconds: int | None = None,
    ) -> None:
        self._cache: OrderedDict[Hashable, tuple[Any, float]] = OrderedDict()
        self._max_size = max_size
        self._ttl = ttl_seconds
        self._lock = asyncio.Lock()

    def _is_expired(self, timestamp: float) -> bool:
        if self._ttl is None:
            return False
        return time.time() - timestamp > self._ttl

    async def get(self, key: Hashable) -> Any | None:
        """获取缓存值，如果不存在或已过期则返回 None.

        命中时会刷新访问时间（滑动过期：自最后一次 get/put 起算 ttl）。
        """
        async with self._lock:
            if key not in self._cache:
                return None

            value, timestamp = self._cache[key]
            if self._is_expired(timestamp):
                self._cache.pop(key, None)
                return None

            # 刷新访问时间并移动到末尾（最近使用）
            self._cache[key] = (value, time.time())
            self._cache.move_to_end(key)
            return value

    async def put(self, key: Hashable, value: Any) -> None:
        """存入缓存值，如果超过容量则淘汰最久未使用的."""
        async with self._lock:
            if key in self._cache:
                self._cache.pop(key)
            elif self._max_size is not None and len(self._cache) >= self._max_size:
                # 淘汰最久未使用的（头部）
                self._cache.popitem(last=False)

            self._cache[key] = (value, time.time())

    async def touch_if_same(self, key: Hashable, value: Any) -> bool:
        """若 key 存在且缓存值与 value 为同一对象，则刷新访问时间.

        用于请求结束时续约 TTL，避免无条件 put 用旧实例覆盖并发创建的新实例。
        同一对象仍挂在 cache 上时，即使时间戳已过期也会续期（执行期间无 get 触达）。
        """
        async with self._lock:
            if key not in self._cache:
                return False

            cached_value, _timestamp = self._cache[key]
            if cached_value is not value:
                return False

            self._cache[key] = (value, time.time())
            self._cache.move_to_end(key)
            return True

    async def remove(self, key: Hashable) -> None:
        """删除缓存项."""
        async with self._lock:
            self._cache.pop(key, None)

    async def clear(self) -> None:
        """清空缓存."""
        async with self._lock:
            self._cache.clear()

    def __len__(self) -> int:
        return len(self._cache)

    def snapshot_values_nowait(self) -> list[Any]:
        """Return cached values for sync callers (best-effort, no async lock).

        Entries are stored as ``(value, timestamp)``; malformed entries are skipped.
        """
        values: list[Any] = []
        for entry in self._cache.values():
            if not isinstance(entry, tuple) or len(entry) < 1:
                continue
            values.append(entry[0])
        return values

    async def keys(self) -> list[Hashable]:
        async with self._lock:
            if self._ttl is not None:
                expired_keys = [
                    key
                    for key, (_, timestamp) in self._cache.items()
                    if self._is_expired(timestamp)
                ]
                for key in expired_keys:
                    del self._cache[key]
            return list(self._cache.keys())

setup_logger()

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
    service_id: str = ""  # 落盘用；空则 normalize 为 default


def resolve_file_transfer_received_dir(
    received_files_dir: str,
    service_id: str | None = None,
) -> Path:
    """Resolve service-scoped received_files root for one transfer.

    - Absolute ``received_files_dir`` (tests / override): use as-is.
    - Relative: ``service_{sid}/<received_files_dir>`` with sid defaulting to ``default``.
    """
    sub = Path(received_files_dir)
    if sub.is_absolute():
        sub.mkdir(parents=True, exist_ok=True)
        return sub
    sid = normalize_tenant_scope_id(service_id)
    path = get_service_root_dir(sid) / sub
    path.mkdir(parents=True, exist_ok=True)
    return path


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
