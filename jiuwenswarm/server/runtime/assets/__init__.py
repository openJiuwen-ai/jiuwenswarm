# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""资产统一导入导出运行时（assets.export / assets.import）。

五类资产（skill / skillpack / agent_template / agent_group / plugin）
共用同一套导出与导入入口，统一包格式、传输通道与错误码，避免各模块
各自实现导致包格式割裂与重复建设。子模块划分：

- ``contract``：kind 枚举、入参出参解析、导出文件名规范（零业务依赖）；
- ``errors``：ASSET_* 统一错误码与入口收敛映射；
- 后续落地的安全打包、kind 注册表与入口 handler 亦挂在本包下。

各 kind 的具体解析/注册逻辑由适配器提供，入口层只做契约校验、
分发与错误收敛，保证新增资产类型不需要改协议。
"""
