# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业运行时平面：一次组装，统一挂载."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from jiuwenswarm.enterprise.edition import is_enterprise

if TYPE_CHECKING:
    from jiuwenswarm.enterprise.config.facade import ConfigFacade
    from jiuwenswarm.enterprise.tenant.tenant_strategy import TenantStrategy
    from jiuwenswarm.runtime.service import AgentRuntime
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

logger = logging.getLogger(__name__)

_PLANE: EnterprisePlane | None = None
_BUILD_ATTEMPTED = False


class EnterprisePlaneError(RuntimeError):
    """企业版平面必须可用但组装失败（禁止静默回退个人版）."""


@dataclass
class EnterprisePlane:
    """AgentServer 企业版能力门面（租户 + 请求读库合并）.

    Gateway 侧管理面路由等见 ``gateway.enterprise``，两平面进程边界分离。
    """

    tenant: TenantStrategy
    config: ConfigFacade

    async def on_runtime_start(self, runtime: AgentRuntime) -> None:
        """Runtime 已在构造期注入 tenant strategy；此处预留扩展挂载."""
        _ = runtime

    async def cleanup(self) -> None:
        await self.tenant.cleanup()


def get_enterprise_plane() -> EnterprisePlane | None:
    """个人版返回 None；企业版返回已组装平面，失败则抛 ``EnterprisePlaneError``."""
    if _PLANE is not None:
        return _PLANE
    if not _BUILD_ATTEMPTED:
        return try_build_enterprise_plane()
    if is_enterprise() and _PLANE is None:
        raise EnterprisePlaneError(
            "AgentServer enterprise plane is required but failed to assemble earlier"
        )
    return None


def require_enterprise_plane() -> EnterprisePlane:
    """企业版路径专用：必须拿到平面，否则报错."""
    plane = get_enterprise_plane()
    if plane is None:
        raise EnterprisePlaneError(
            "AgentServer enterprise plane is required but unavailable "
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


def try_build_enterprise_plane(
    *,
    default_manager: AgentManager | None = None,
    force: bool = False,
) -> EnterprisePlane | None:
    """个人版返回 None；企业版组装成功返回平面，失败抛 ``EnterprisePlaneError``.

    Multi / Config 经本函数懒加载，避免个人版 import 拉起 Pool/GDB。
    """
    global _PLANE, _BUILD_ATTEMPTED

    if _PLANE is not None and not force:
        if default_manager is not None:
            _PLANE.tenant.adopt_default_manager(default_manager)
        return _PLANE

    if _BUILD_ATTEMPTED and not force:
        if is_enterprise() and _PLANE is None:
            raise EnterprisePlaneError(
                "AgentServer enterprise plane is required but failed to assemble earlier"
            )
        return _PLANE

    _BUILD_ATTEMPTED = True
    if not is_enterprise():
        logger.debug("[EnterprisePlane] skipped: not enterprise edition")
        return None

    try:
        from jiuwenswarm.enterprise.config.facade import ConfigFacade
        from jiuwenswarm.enterprise.tenant.multi_tenant_strategy import MultiTenantStrategy
        from jiuwenswarm.server.runtime.agent_manager import AgentManager

        tenant = MultiTenantStrategy()
        seeded = default_manager or AgentManager()
        tenant.adopt_default_manager(seeded)
        plane = EnterprisePlane(tenant=tenant, config=ConfigFacade())
        _PLANE = plane
        logger.info("[EnterprisePlane] assembled (tenant=Multi, config=Facade)")
        return plane
    except EnterprisePlaneError:
        _PLANE = None
        raise
    except Exception as exc:
        _PLANE = None
        logger.exception("[EnterprisePlane] build failed")
        raise EnterprisePlaneError(
            "AgentServer enterprise plane failed to assemble"
        ) from exc


def resolve_tenant_strategy(
    *,
    agent_manager: AgentManager | None = None,
) -> TenantStrategy:
    """供 AgentRuntime 组装：企业平面 Multi，否则 Single（可不建 Pool）.

    企业版平面组装失败向上抛错，不会回退 Single。
    """
    from jiuwenswarm.enterprise.tenant.single_tenant_strategy import SingleTenantStrategy
    from jiuwenswarm.server.runtime.agent_manager import AgentManager

    plane = try_build_enterprise_plane(default_manager=agent_manager)
    if plane is not None:
        if agent_manager is not None:
            plane.tenant.adopt_default_manager(agent_manager)
        return plane.tenant

    return SingleTenantStrategy(agent_manager or AgentManager())
