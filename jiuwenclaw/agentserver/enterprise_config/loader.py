"""从 Gateway DB 加载企业级生效配置（委托 ``manager_ws_client`` 实现）。"""

from __future__ import annotations

import os

from jiuwenclaw.infrastructure.module_importer import (
    import_manager_ws_client_module,
)

_gateway_db_mod = import_manager_ws_client_module("core.enterprise_config.gateway_db")
GatewayDb = _gateway_db_mod.GatewayDb
schemas = import_manager_ws_client_module("core.enterprise_config.schemas")
loader = import_manager_ws_client_module("core.enterprise_config.loader")

_jiuwenclaw_id = os.getenv("JIUWENCLAW_ID", "").strip() or None
gateway_db = GatewayDb.bind(_jiuwenclaw_id)

EffectiveEnterpriseConfig = schemas.EffectiveEnterpriseConfig
DEFAULT_AGENT_LOAD_SLOTS = schemas.DEFAULT_AGENT_LOAD_SLOTS
TemplateRefSlot = schemas.TemplateRefSlot
load_effective_enterprise_config = loader.load_effective_enterprise_config
invalidate_policy_snapshot = loader.invalidate_policy_snapshot


__all__ = (
    "DEFAULT_AGENT_LOAD_SLOTS",
    "EffectiveEnterpriseConfig",
    "TemplateRefSlot",
    "invalidate_policy_snapshot",
    "load_effective_enterprise_config",
)
