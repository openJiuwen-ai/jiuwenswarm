# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.

"""Logging level configuration."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

from ruamel.yaml import YAML


@dataclass
class LoggingLevels:
    """Container for logging level configuration."""

    logger: int
    console: int
    gateway: int
    channel: int
    agent_server: int


def parse_log_level(name: str, default: int = logging.INFO) -> int:
    if not name or not isinstance(name, str):
        return default
    return getattr(logging, name.strip().upper(), default)


def _load_logging_config_from_yaml() -> dict[str, Any]:
    from jiuwenclaw.utils import get_config_file

    try:
        cf = get_config_file()
        if not cf.exists():
            return {}
        rt = YAML()
        with open(cf, "r", encoding="utf-8") as f:
            data = rt.load(f) or {}
        raw = data.get("logging")
        if isinstance(raw, dict):
            return raw
    except Exception as e:
        logging.getLogger(__name__).error("load logging config failed, caused by=%s", e)
    return {}


def resolve_logging_levels(log_level_override: Optional[str]) -> LoggingLevels:
    cfg = _load_logging_config_from_yaml()
    base = parse_log_level(str(cfg.get("level", "INFO")))

    def _coerce(key: str) -> int:
        if key in cfg and cfg[key] is not None:
            return parse_log_level(str(cfg[key]), base)
        return base

    console = _coerce("console_level")
    gateway = _coerce("gateway")
    channel = _coerce("channel")
    agent_server = _coerce("agent_server")

    if log_level_override is not None:
        v = parse_log_level(log_level_override)
        console = gateway = channel = agent_server = v
        logger_level = v
    else:
        env_level = os.getenv("LOG_LEVEL")
        if env_level:
            v = parse_log_level(env_level, base)
            console = gateway = channel = agent_server = v
        logger_level = min(gateway, channel, agent_server)

    return LoggingLevels(logger_level, console, gateway, channel, agent_server)
