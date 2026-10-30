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
