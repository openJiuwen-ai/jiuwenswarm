# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 日志初始化（``gateway.common.paths.setup_logger`` 的 Gateway 版本）。

输出目录与母仓一致：``~/.jiuwenswarm/agent/.logs/``；``gateway.log`` 可通过
``AGENTOS_GATEWAY_LOG_DIR`` 独立存放。级别取自共享 ``config.yaml`` 的
``logging`` 段。handler / 级别 / 过滤器的定义与母仓逐行一致，仅根 logger
从 ``jiuwenswarm`` 换成 ``gateway``。
"""

from __future__ import annotations

import datetime
import logging
import os
import re
import shutil
import sys
from dataclasses import dataclass
from logging.handlers import BaseRotatingHandler
from pathlib import Path
from typing import Any, Optional

from ruamel.yaml import YAML

from gateway.common.log_sanitize import (
    JsonOnlyFormatter,
    SensitiveDataFilter,
    install_source_record_masking,
)
from gateway.common.paths import (
    get_config_file,
    get_gateway_log_dir,
    get_logs_dir,
)
from gateway.common.runtime_log_filter import install_runtime_log_filter

logger = logging.getLogger(__name__)

# 单文件轮转阈值与保留份数（与母仓一致）
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

        timestamp = datetime.datetime.now(tz=datetime.timezone.utc).strftime("%Y%m%d_%H%M%S")
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
    """按 ``logging.getLogger(__name__)`` 的 logger 名划分 gateway / channel / agent_server / permissions（含 security）。"""
    if name.startswith("jiuwenswarm.channels"):
        return "channel"
    if name.startswith("jiuwenswarm.agents.harness.common.rails.permissions"):
        return "permissions"
    if name.startswith("openjiuwen.harness.security") or name.startswith("openjiuwen.harness.rails.security"):
        return "permissions"
    if name.startswith("jiuwenswarm.agents") or name.startswith("jiuwenswarm.server"):
        return "agent_server"
    return "gateway"


class _ComponentNameFilter(logging.Filter):
    """仅放行指定组件（由 logger 名判定）的日志记录。"""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        return _log_component_from_logger_name(record.name) == self.component


class _ExcludeComponentFilter(logging.Filter):
    """仅拦截指定组件（由 logger 名判定）的日志记录，其余全部放行。"""

    def __init__(self, component: str) -> None:
        super().__init__()
        self.component = component

    def filter(self, record: logging.LogRecord) -> bool:
        return _log_component_from_logger_name(record.name) != self.component


class _CompositeFilter(logging.Filter):
    """组合多个过滤器，任一通过即放行"""

    def __init__(self, filters: list[logging.Filter]) -> None:
        super().__init__()
        self.filters = filters

    def filter(self, record: logging.LogRecord) -> bool:
        return any(f.filter(record) for f in self.filters)


def _load_logging_config_from_yaml() -> dict[str, Any]:
    """读取 ~/.jiuwenswarm/config/config.yaml 中的 logging 段（无则空）。"""
    try:
        cf = get_config_file()
        if not cf.exists():
            return {}
        # Logging reads values only. Round-trip comment preservation makes
        # importing this module parse the entire config on the Front startup path.
        rt = YAML(typ="safe")
        content = cf.read_text(encoding="utf-8")
        section = re.search(r'(?m)^(?:logging|[\"\']logging[\"\'])\s*:', content)
        if section is None:
            # A valid flow mapping need not have a standalone logging section.
            data = rt.load(content) or {}
        else:
            boundary = re.search(r"(?m)^[^\s#]", content[section.end():])
            end = section.end() + boundary.start() if boundary else len(content)
            try:
                data = rt.load(content[section.start():end]) or {}
            except Exception:
                # A logging alias may reference an anchor from another section.
                data = YAML(typ="safe").load(content) or {}
        if not isinstance(data, dict):
            return {}
        raw = data.get("logging")
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        logger.warning("load logging config failed, caused by=%s", e)
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

    if log_level_override is not None:
        v = _parse_log_level(log_level_override)
        console = gateway = channel = agent_server = full = v
        logger_level = v
    else:
        logger_level = min(gateway, channel, agent_server, full)

    return LoggingLevels(logger_level, console, gateway, channel, agent_server, full)


def _reconfigure_stdio_utf8() -> None:
    """把 ``sys.stdout`` / ``sys.stderr`` 原地重配置为 UTF-8。

    Windows 控制台默认编码常为 cp1252，无法编码中文日志消息（例如扩展加载器的
    ``[ExtensionLoader] 开始搜索扩展路径``），会导致 ``logging.StreamHandler.emit``
    抛出 ``UnicodeEncodeError``；随后的 ``logging.handleError`` 想把异常栈打印到
    ``sys.stderr``，又因同一编码问题二次失败，连锁中断启动。这里在日志体系初始化前
    把标准流原地改为 UTF-8 + ``backslashreplace``，覆盖 emit / handleError / print 三个路径。
    """
    for _stream in (sys.stdout, sys.stderr):
        _reconfigure = getattr(_stream, "reconfigure", None)
        if not callable(_reconfigure):
            continue
        try:
            _reconfigure(encoding="utf-8", errors="backslashreplace")
        except (ValueError, OSError, RuntimeError):
            # 流已被使用 / 不支持重配置：忽略，保留原流，避免影响启动。
            pass


_GATEWAY_LOGGING_CONFIGURED_ATTR = "_gateway_logging_configured"


def _is_gateway_logging_configured(target: logging.Logger) -> bool:
    """幂等标记放在 logger 实例上，模块 reload 后仍能生效。"""
    return bool(getattr(target, _GATEWAY_LOGGING_CONFIGURED_ATTR, False))


def _mark_gateway_logging_configured(target: logging.Logger) -> None:
    setattr(target, _GATEWAY_LOGGING_CONFIGURED_ATTR, True)


def setup_gateway_logger(log_level: Optional[str] = None) -> logging.Logger:
    """配置 ``gateway`` 根日志：控制台 + 分组件文件 + 汇总 full.log。

    文件、目录、级别语义与母仓 ``setup_logger`` 保持一致；区别是本函数只负责
    ``gateway`` 这一棵 logger 树，不再向上挂到 ``jiuwenswarm``。

    幂等。共同安装场景下母仓 ``setup_logger`` 已把 ``gateway`` logger 挂到
    ``jiuwenswarm`` 根并装好 handler，此时直接复用，避免同一批日志重复落盘；
    独立安装（无母仓 handler）时由本函数完成配置。
    """
    root = logging.getLogger("gateway")
    if _is_gateway_logging_configured(root):
        return root

    if logging.getLogger("jiuwenswarm").handlers:
        _mark_gateway_logging_configured(root)
        return root

    install_runtime_log_filter()
    _reconfigure_stdio_utf8()
    logs_root = get_logs_dir()
    logs_root.mkdir(parents=True, exist_ok=True)

    levels = _resolve_logging_levels(log_level)
    root.setLevel(levels.logger)
    root.propagate = False
    for handler in root.handlers[:]:
        handler.close()
        root.removeHandler(handler)

    formatter = logging.Formatter(
        fmt="%(asctime)s.%(msecs)03d %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    privacy_filter = SensitiveDataFilter()

    def _add_rotating(
        filename: str,
        level: int,
        name_filter: Optional[logging.Filter] = None,
        custom_formatter: Optional[logging.Formatter] = None,
        target_dir: Optional[Path] = None,
    ) -> None:
        base_dir = target_dir if target_dir is not None else logs_root
        h = SafeRotatingFileHandler(
            filename=base_dir / filename,
            maxBytes=_LOG_FILE_MAX_BYTES,
            backupCount=_LOG_FILE_BACKUP_COUNT,
            encoding="utf-8",
        )
        h.setLevel(level)
        h.setFormatter(custom_formatter if custom_formatter is not None else formatter)
        if name_filter is not None:
            h.addFilter(name_filter)
        h.addFilter(privacy_filter)
        root.addHandler(h)

    gateway_log_dir = get_gateway_log_dir()
    if gateway_log_dir is not None:
        try:
            gateway_log_dir.mkdir(parents=True, exist_ok=True)
            probe = gateway_log_dir / ".write_probe"
            probe.touch()
            probe.unlink()
        except OSError as exc:
            print(
                f"[gateway] gateway log dir {gateway_log_dir} is not writable ({exc}); "
                f"falling back to {logs_root}",
                file=sys.stderr,
            )
            gateway_log_dir = None

    _add_rotating(
        "gateway.log", levels.gateway, _ComponentNameFilter("gateway"),
        target_dir=gateway_log_dir,
    )
    _add_rotating("channel.log", levels.channel, _ComponentNameFilter("channel"))
    _add_rotating(
        "agent_server.log", levels.agent_server,
        _CompositeFilter([_ComponentNameFilter("agent_server"), _ComponentNameFilter("permissions")]),
    )
    _add_rotating("full.log", levels.full, _ExcludeComponentFilter("gateway"))
    _add_rotating(
        "permissions.log", levels.agent_server,
        _ComponentNameFilter("permissions"), JsonOnlyFormatter(),
    )

    stream_handler = logging.StreamHandler()
    stream_handler.setLevel(levels.console)
    stream_handler.setFormatter(formatter)
    stream_handler.addFilter(privacy_filter)
    root.addHandler(stream_handler)

    install_source_record_masking()
    _mark_gateway_logging_configured(root)
    return root


__all__ = ["setup_gateway_logger"]
