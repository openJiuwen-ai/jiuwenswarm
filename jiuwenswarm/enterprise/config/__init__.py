# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业平面 · 配置子系统（AgentServer）.

- ``facade``：请求时 load_and_merge
- ``config_provider``：resolve_agent_config / 企业策略合并
- ``effective``：读库 / apply_*（GDB 有效配置）
"""

from jiuwenswarm.enterprise.config.facade import ConfigFacade

__all__ = ["ConfigFacade"]
