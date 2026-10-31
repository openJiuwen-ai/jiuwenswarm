# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业平面 · 租户子系统.

``TenantAgentPool`` / Strategy 等重依赖经 ``__getattr__`` 懒加载，避免
``common.utils`` 再导出 ``tenant_paths`` 时拉起 AgentManager / schema。
"""

from __future__ import annotations

from typing import Any

from jiuwenswarm.enterprise.tenant.tenant_context import TenantContext
from jiuwenswarm.enterprise.tenant.tenant_identity import (
    bind_control_tenant,
    extract_tenant_key,
    extract_triple,
)
from jiuwenswarm.enterprise.tenant.tenant_paths import (
    build_tenant_key,
    get_multi_tenant_user_workspace_dir,
    normalize_tenant_key_part,
    seed_tenant_agent_workspace,
)

__all__ = [
    "TenantAgentPool",
    "TenantContext",
    "TenantStrategy",
    "SingleTenantStrategy",
    "MultiTenantStrategy",
    "bind_control_tenant",
    "bind_request_tenant",
    "build_tenant_key",
    "extract_tenant_key",
    "extract_triple",
    "get_multi_tenant_user_workspace_dir",
    "normalize_tenant_key_part",
    "seed_tenant_agent_workspace",
]


def __getattr__(name: str) -> Any:
    if name == "TenantAgentPool":
        from jiuwenswarm.enterprise.tenant.tenant_agent_pool import TenantAgentPool

        return TenantAgentPool
    if name == "TenantStrategy":
        from jiuwenswarm.enterprise.tenant.tenant_strategy import TenantStrategy

        return TenantStrategy
    if name == "SingleTenantStrategy":
        from jiuwenswarm.enterprise.tenant.single_tenant_strategy import SingleTenantStrategy

        return SingleTenantStrategy
    if name == "MultiTenantStrategy":
        from jiuwenswarm.enterprise.tenant.multi_tenant_strategy import MultiTenantStrategy

        return MultiTenantStrategy
    if name == "bind_request_tenant":
        from jiuwenswarm.enterprise.tenant.bind_request import bind_request_tenant

        return bind_request_tenant
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
