# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Offline flash static-prompt assembler（M0 S1 离线组装）。

用**真实 Deep 提示词构建函数** + **flash content 域常量/纯函数**，在离线（无运行时
状态）下确定性重建 flash 自有静态段。组装逻辑已收敛到
:mod:`flash.prompt.registry` 的 :func:`reconcile`（声明式注册表驱动），本模块仅保留
离线入口 ``assemble`` —— 与运行时 Composer 共用同一实现，保证零漂移。

v2 覆盖（确定性、平台无关）：safety / response / tools / todo / subagent 五域，
共 14 段（见 :data:`SNAPSHOT_DOMAINS` 与 registry ``FLASH_PROMPT_REGISTRY``）。

v2 仍排除（各自原因）：
- **persona 域**：identity 源正文依赖 ``sys.platform`` / shell 环境 / 配置目录 / 沙箱
  开关，跨 dev/CI 不可复现；其 flash 自有常量（020/200/220/400）已由 ``test_flash_persona_rail``
  锁定。待 registry 的 ``PromptRenderContext.extra`` 支持显式平台注入后再纳入快照。
- **relay_merge / skill**：归 relay（relay TS 内容）或内容动态，留待后续重构步纳入。

本模块在 CI（``agent_shield`` 存在）下可直接 import；本地生成快照见
``tests/unit_tests/agentserver/flash_snapshot/README.md``。
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.flash.prompt.registry import (
    build_default_context,
    reconcile,
)

__all__ = ["LANGUAGES", "SNAPSHOT_DOMAINS", "assemble"]

# 快照覆盖的语言与域（v2）。
LANGUAGES = ("cn", "en")
SNAPSHOT_DOMAINS = ("safety", "response", "tools", "todo", "subagent")


def assemble(language: str = "cn"):
    """重建 flash 自有静态段，返回 ``PromptSection`` 列表，按段号（priority）升序。

    委托 :func:`flash.prompt.registry.reconcile`（默认上下文三工具域全开），与运行时
    Composer 的静态组装同源。
    """
    return reconcile(build_default_context(language))
