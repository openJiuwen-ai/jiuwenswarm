# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 数据建模框架预置插件。

此目录下只放框架预置的组件(如 BlankDataModeler)。
检测模块专属的建模插件放在 detection_modules/<module_name>/ 下。
"""

from agent_ssas.core.framework.data_modeler.plugins.blank_data_modeler import BlankDataModeler

__all__ = [
    "BlankDataModeler",
]
