# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 企业平面：管理面路由等网关侧企业能力的组装入口.

与 ``jiuwenswarm.enterprise``（AgentServer 租户 / 读库合并）分离，避免 gateway
拆仓后依赖 AgentServer 包内企业平面。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from gateway.enterprise.edition import is_enterprise

if TYPE_CHECKING:
    from gateway.enterprise.config.facade import GatewayConfigFacade

logger = logging.getLogger(__name__)

_PLANE: GatewayEnterprisePlane | None = None
_BUILD_ATTEMPTED = False


class EnterprisePlaneError(RuntimeError):
    """企业版平面必须可用但组装失败（禁止静默回退个人版）."""


@dataclass
class GatewayEnterprisePlane:
    """Gateway 进程企业能力门面."""

    config: GatewayConfigFacade

    def register_routes(self, app: Any) -> None:
        """挂载管理面等企业 HTTP 路由到统一 WEB_PORT."""
        self.config.register_manager_routes(app)

    def install_web_channel(self, app: Any, channel: Any) -> None:
        """WebChannel 上的企业增量挂载（管理面 + 企业 HTTP/SSE/文件）.

        个人版不调用本方法；企业版由调用方在平面就绪后安装。
        """
        self.register_routes(app)
        from gateway.channel_manager.web.web_http_routes import (
            register_web_http_api_routes,
        )

        register_web_http_api_routes(app, channel, enterprise=True)
        from gateway.channel_manager.web.enterprise_files import (
            register_enterprise_file_routes,
        )

        register_enterprise_file_routes(app, channel)
        logger.info(
            "WebChannel enterprise HTTP/SSE mounted on same port "
            "(/api/v1 health·sessions·chat/completions + mapped, no A2A)",
        )


def get_enterprise_plane() -> GatewayEnterprisePlane | None:
    """个人版返回 None；企业版返回已组装平面，失败则抛 ``EnterprisePlaneError``."""
    if _PLANE is not None:
        return _PLANE
    if not _BUILD_ATTEMPTED:
        return try_build_enterprise_plane()
    if is_enterprise() and _PLANE is None:
        raise EnterprisePlaneError(
            "Gateway enterprise plane is required but failed to assemble earlier"
        )
    return None


def require_enterprise_plane() -> GatewayEnterprisePlane:
    """企业版路径专用：必须拿到平面，否则报错."""
    plane = get_enterprise_plane()
    if plane is None:
        raise EnterprisePlaneError(
            "Gateway enterprise plane is required but unavailable "
            "(not enterprise edition or not assembled)"
        )
    return plane


def enterprise_enabled() -> bool:
    """平面是否已组装（仅用于依赖平面的路径）.

    企业版若组装失败会抛 ``EnterprisePlaneError``，不会返回 False 伪装成个人版。
    产品形态开关请用 ``is_enterprise()``（edition）。
    """
    return get_enterprise_plane() is not None


def reset_enterprise_plane() -> None:
    """测试用：清空平面单例."""
    global _PLANE, _BUILD_ATTEMPTED
    _PLANE = None
    _BUILD_ATTEMPTED = False


def try_build_enterprise_plane(*, force: bool = False) -> GatewayEnterprisePlane | None:
    """个人版返回 None；企业版组装成功返回平面，失败抛 ``EnterprisePlaneError``."""
    global _PLANE, _BUILD_ATTEMPTED

    if _PLANE is not None and not force:
        return _PLANE
    if _BUILD_ATTEMPTED and not force:
        if is_enterprise() and _PLANE is None:
            raise EnterprisePlaneError(
                "Gateway enterprise plane is required but failed to assemble earlier"
            )
        return _PLANE

    _BUILD_ATTEMPTED = True
    if not is_enterprise():
        logger.debug("[GatewayEnterprisePlane] skipped: not enterprise edition")
        return None

    try:
        from gateway.enterprise.config.facade import GatewayConfigFacade

        plane = GatewayEnterprisePlane(config=GatewayConfigFacade())
        _PLANE = plane
        logger.info("[GatewayEnterprisePlane] assembled")
        return plane
    except EnterprisePlaneError:
        _PLANE = None
        raise
    except Exception as exc:
        _PLANE = None
        logger.exception("[GatewayEnterprisePlane] build failed")
        raise EnterprisePlaneError(
            "Gateway enterprise plane failed to assemble"
        ) from exc
