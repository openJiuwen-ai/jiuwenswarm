# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-side utils facade: path contracts + logging + tenant helpers.

路径推导真源在 ``gateway.common.paths``（!9120 建立）；本模块补充
enterprise 代码所需的 ``apply_logging_config_payload`` /
``build_tenant_key`` / ``get_config_file`` / ``logger``。
"""

from __future__ import annotations

import logging

from gateway.common.paths import (  # noqa: F401
    get_agent_home_dir,
    get_agent_root_dir,
    get_agent_sessions_dir,
    get_agent_workspace_dir,
    get_config_dir,
    get_logs_dir,
    get_root_dir,
    get_user_home,
    get_user_workspace_dir,
    set_user_home,
)

logger = logging.getLogger(__name__)


def get_config_file() -> "Path":
    """Return the path to the user's config.yaml."""
    from gateway.common.paths import get_config_dir

    return get_config_dir() / "config.yaml"


def apply_logging_config_payload(payload: dict) -> None:
    """Apply logging config from config.yaml's logging section."""
    import logging.config

    if not isinstance(payload, dict) or not payload:
        return
    try:
        logging.config.dictConfig(payload)
    except (ValueError, TypeError, KeyError) as exc:
        logger.warning("failed to apply logging config payload: %s", exc)


def build_tenant_key(tenant_id: str) -> str:
    """Build a normalized tenant key for enterprise storage paths."""
    from gateway.enterprise.tenant.tenant_paths import build_tenant_key as _real

    return _real(tenant_id)


from pathlib import Path  # noqa: E402


def get_env_file() -> Path:
    return get_config_dir() / ".env"


def update_log_levels(
    log_level: Optional[str] = None,
    *,
    console_level: Optional[str] = None,
    gateway: Optional[str] = None,
    channel: Optional[str] = None,
    agent_server: Optional[str] = None,
    full: Optional[str] = None,
) -> logging.Logger:
    """运行时动态更新 ``jiuwenswarm`` 根日志及各 handler 的级别，无需重建 handler。"""
    levels = _resolve_logging_levels(log_level)

    if console_level is not None:
        levels = replace(levels, console=_parse_log_level(console_level, levels.console))
    if gateway is not None:
        levels = replace(levels, gateway=_parse_log_level(gateway, levels.gateway))
    if channel is not None:
        levels = replace(levels, channel=_parse_log_level(channel, levels.channel))
    if agent_server is not None:
        levels = replace(levels, agent_server=_parse_log_level(agent_server, levels.agent_server))
    if full is not None:
        levels = replace(levels, full=_parse_log_level(full, levels.full))

    logger_level = min(levels.gateway, levels.channel, levels.agent_server, levels.full)
    levels = replace(levels, logger=logger_level)

    root = logging.getLogger("jiuwenswarm")
    root.setLevel(levels.logger)

    from jiuwenswarm.common.background_logging import _QueuedHandler

    def update_handler(h: logging.Handler) -> None:
        if isinstance(h, _QueuedHandler):
            for target in h.handlers:
                update_handler(target)
            h.setLevel(min(target.level for target in h.handlers))
        elif isinstance(h, SafeRotatingFileHandler):
            attr = _FILE_HANDLER_LEVEL_MAP.get(Path(h.baseFilename).name)
            if attr is not None:
                h.setLevel(getattr(levels, attr))
        elif isinstance(h, logging.StreamHandler):
            h.setLevel(levels.console)

    for h in root.handlers:
        update_handler(h)
    return root


def get_checkpoint_dir() -> Path:
    _migrate_legacy_checkpoint_and_logs()
    return get_agent_root_dir() / ".checkpoint"


def _logging_config_row_to_dict(obj: dict[str, Any] | Any) -> dict[str, Any]:
    if isinstance(obj, dict):
        return {
            "level": obj.get("level", "INFO"),
            "console_level": obj.get("console_level"),
            "gateway": obj.get("gateway"),
            "channel": obj.get("channel"),
            "agent_server": obj.get("agent_server"),
            "full": obj.get("full"),
        }
    return {
        "level": getattr(obj, "level", "INFO"),
        "console_level": getattr(obj, "console_level", None),
        "gateway": getattr(obj, "gateway", None),
        "channel": getattr(obj, "channel", None),
        "agent_server": getattr(obj, "agent_server", None),
        "full": getattr(obj, "full", None),
    }
