# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 检测模块管理器。

管理检测模块的完整生命周期:扫描、加载、注册、订阅管理、存储管理。
初始化时扫描 detection_modules/ 目录,加载所有模块的配置和插件。
流水线执行时提供订阅查询接口。
"""

from agent_ssas.core.framework.module_manager.manager import (
    DetectionModule,
    DetectionModuleManager,
)

__all__ = [
    "DetectionModule",
    "DetectionModuleManager",
]
