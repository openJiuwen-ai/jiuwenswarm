# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.
"""Bootstrap openjiuwen file logging under ``agent/.logs/openjiuwen``.

Call after workspace is ready and before importing modules that may trigger
openjiuwen loggers (otherwise default ``./logs`` is created under the process
cwd, often ``~/.jiuwenswarm/logs``).
"""

from __future__ import annotations

import logging
from typing import Any


def _pin_openjiuwen_log_path(log_root) -> None:
    """Pin openjiuwen log files under ``log_root`` (compat across openjiuwen versions)."""
    from openjiuwen.core.common.logging.log_config import (
        configure_log_config,
        get_log_config_snapshot,
    )

    target = str(log_root)
    try:
        from openjiuwen.core.common.logging.log_config import set_log_path

        set_log_path(log_root)
        return
    except ImportError:
        pass

    config = get_log_config_snapshot()
    if config.get("log_path") == target:
        return
    config["log_path"] = target
    configure_log_config(config)


def bootstrap_openjiuwen_logging() -> bool:
    """Optionally load logging.yaml, pin log_path, and set default levels.

    Personal edition keeps the previous behavior: without ``logging.yaml``,
    every logger is INFO. Enterprise hot-reload applies the manager level
    later via :func:`apply_openjiuwen_log_level`.

    Returns:
        True if ``config/logging.yaml`` was loaded; False otherwise.
    """
    from openjiuwen.core.common.logging import LogManager
    from openjiuwen.core.common.logging.log_config import configure_log

    from jiuwenswarm.common.utils import get_logs_dir, get_root_dir

    logging_yaml = get_root_dir() / "config" / "logging.yaml"
    loaded_yaml = logging_yaml.is_file()
    if loaded_yaml:
        configure_log(str(logging_yaml))

    # Always override path so hosts never depend on cwd-relative ./logs/
    log_root = get_logs_dir() / "openjiuwen"
    log_root.mkdir(parents=True, exist_ok=True)
    _pin_openjiuwen_log_path(log_root)

    if not loaded_yaml:
        for logger in LogManager.get_all_loggers().values():
            logger.set_level(logging.INFO)

    return loaded_yaml


def apply_openjiuwen_log_level(level: int) -> None:
    """Set every openjiuwen logger to ``level``.

    Writes the level into the in-memory log config so loggers created later
    inherit it, and updates loggers that already exist. Does not recreate
    handlers. Per-logger level overrides are cleared so the managed level
    is the one that takes effect.

    ``level`` is the resolved AgentServer level from the manager logging
    config (``agent_server``, falling back to ``level``).
    """
    from openjiuwen.core.common.logging.log_config import (
        get_log_config_snapshot,
        log_config,
    )
    from openjiuwen.core.common.logging.manager import LogManager

    normalized = int(level)
    config = get_log_config_snapshot()
    _stamp_managed_level(config, normalized)
    log_config.load_from_dict(config)

    # Skip live updates when the manager is not yet initialized. Prefer the
    # public probe (G.CLS.11); older openjiuwen falls through to get_all_loggers.
    is_initialized = getattr(LogManager, "is_initialized", None)
    if callable(is_initialized) and not is_initialized():
        return
    for logger in LogManager.get_all_loggers().values():
        set_level = getattr(logger, "set_level", None)
        if callable(set_level):
            set_level(normalized)


def _stamp_managed_level(config: dict[str, Any], level: int) -> None:
    """Make ``level`` the only effective level in a logging config snapshot."""
    config["level"] = level
    defaults = config.get("defaults")
    if isinstance(defaults, dict):
        defaults["level"] = level
    loggers = config.get("loggers")
    if isinstance(loggers, dict):
        for logger_cfg in loggers.values():
            if isinstance(logger_cfg, dict):
                logger_cfg.pop("level", None)
    sinks = config.get("sinks")
    if isinstance(sinks, dict):
        sink_items = sinks.values()
    elif isinstance(sinks, list):
        sink_items = sinks
    else:
        return
    for sink_cfg in sink_items:
        if isinstance(sink_cfg, dict) and "level" in sink_cfg:
            sink_cfg["level"] = level
