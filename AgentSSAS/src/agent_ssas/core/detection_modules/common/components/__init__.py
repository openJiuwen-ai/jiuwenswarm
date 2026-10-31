# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""AgentSSAS 通用检测组件目录。

此目录下放置通用数据建模组件和威胁检测组件,供其他检测模块
在 module.yaml 中声明引用。0.1 版本为预留目录,后续版本扩展通用组件。

通用组件的使用方式:
  - 检测模块在 module.yaml 的 plugins 段引用通用组件的类名
  - DetectionModuleManager 实例化时会先检查框架预置插件目录,
    再检查模块专属目录,最后检查此目录(后续版本实现)
"""
