# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 配置模块。

从 jiuwenswarm 的 config.yaml 的 ssas 段加载配置,
环境变量覆盖(SSAS_ 前缀),注入到各模块。
"""

from agent_ssas.core.framework.config.settings import AgentSSASConfig, AgentSSASMode

__all__ = ["AgentSSASConfig", "AgentSSASMode"]
