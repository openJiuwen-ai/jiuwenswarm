# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""企业版北向方法白名单，Gateway 与 AgentServer 共用同一份。"""

from __future__ import annotations

#: 企业版经 SPI 开放只读列表/详情与用户自装/卸载/更新写操作；技能卸载走
#: ``skills.enterprise.uninstall``。
ENTERPRISE_SKILL_ALLOWED_METHODS = frozenset({
    "skills.list",
    "skills.get",
    "skills.toggle",
    "skills.source.providers",
    "skills.source.search",
    "skills.source.install",
    "skills.updates.check",
    "skills.update",
    "skills.enterprise.list",
    "skills.enterprise.install",
    "skills.enterprise.uninstall",
    "skills.enterprise.source.providers",
    "skills.enterprise.source.search",
})
