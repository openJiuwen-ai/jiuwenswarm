# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 企业配置门面：管理面路由挂载（同 WEB_PORT）."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class GatewayConfigFacade:
    """仅覆盖 Gateway 进程内的管理面下发能力；不含 AgentServer 读库合并."""

    @staticmethod
    def register_manager_routes(app: Any) -> None:
        try:
            from gateway.enterprise.config.manager_config import (
                register_manager_config_routes,
            )
        except ImportError:
            logger.warning(
                "[GatewayEnterprisePlane] manager_config unavailable; skip route mount"
            )
            return
        register_manager_config_routes(app)
