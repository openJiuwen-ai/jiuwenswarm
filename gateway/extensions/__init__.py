# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-owned extension assets: SSH auth suite and application plugin host.

从 ``jiuwenswarm/extensions`` 吸收的 gateway 专属件（B2）：auth 认证套件
（SSH 协议通道宿主）与 application_host（应用插件 HTTP/WS 宿主，M0-b 起
为本包唯一真源——extensions/ 侧兼容 shim 已删除）。共享的
hook_event / hooks_context 已下沉 ``jiuwenswarm.common``；
加载治理见 ``extensions/manager.py`` 的清单化配置。

解耦阶段（M 组 extensions）：
- ``agentos/config_updater``、``agentos/registry_watcher``、
  ``agentos/agentos_router.{agent_manager,models,logutil}`` 与
  ``video_duplex/backend/tasks/bridge`` 以本包为**唯一实现**，
  ``jiuwenswarm.extensions`` 侧已删除原文件并反向 import 本包；
- ``registry`` 框架、``sdk``、``types``、``callback_compat`` 仍保留本包
  副本：这些是母仓 AgentServer/worker/runtime 同样依赖的共享能力，由母仓
  持有真源、gateway 仅借用，避免母仓为通用能力反向依赖 gateway；
- ``jiuwenswarm.extensions`` 仍作为扩展发现/加载入口（manager/loader 暂留母仓）。

没有本文件时 setuptools.packages.find 不会把 gateway/extensions 当作包，
wheel 中该目录 0 个文件——部署路径（pip install wheel 后启动）加载 SSH
协议时即 ModuleNotFoundError。B2 合入版（dd67687ba）遗漏了此文件，本
提交补齐（与 !8044 最终版 9ca1d5f3e 一致，该 force-push 评审未及合入）。
"""
