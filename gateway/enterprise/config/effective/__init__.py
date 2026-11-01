# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""从 Gateway 本地库解析企业级配置生效策略与模板（Gateway 企业平面）.

与 AS ``jiuwenswarm.enterprise.config.effective`` 语义对齐的独立副本；
Gateway 进程禁止依赖 ``jiuwenswarm.enterprise``。
"""

from gateway.enterprise.config.effective.loader import (
    DEFAULT_AGENT_LOAD_SLOTS,
    invalidate_enterprise_config_caches,
    invalidate_template_entity_cache,
    load_effective_enterprise_config,
)

__all__ = [
    "DEFAULT_AGENT_LOAD_SLOTS",
    "invalidate_enterprise_config_caches",
    "invalidate_template_entity_cache",
    "load_effective_enterprise_config",
]
