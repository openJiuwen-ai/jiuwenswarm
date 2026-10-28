# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-owned extension assets: SSH auth suite and application plugin host.

从 ``jiuwenswarm/extensions`` 吸收的 gateway 专属件（B2）：auth 认证套件
（SSH 协议通道宿主）与 application_host（应用插件 HTTP/WS 宿主，M0-b 起
为本包唯一真源——extensions/ 侧兼容 shim 已删除）。共享的
hook_event / hooks_context 已下沉 ``jiuwenswarm.common``；
加载治理见 ``extensions/manager.py`` 的清单化配置。

解耦阶段（M 组 extensions）：
- ``agentos/config_updater``、``agentos/registry_watcher``、
  ``agentos/agentos_router``（含 agent_manager/models/logutil、router/registry
  client、ssh_relay、stale_cleanup、third_agent 与扩展入口）、``adx``、
  ``agent_client``、``yuanrong_frontend_client``、
  ``video_duplex/backend/tasks/bridge`` 以本包为**唯一实现**，
  ``jiuwenswarm.extensions`` 侧已删除原文件并反向 import 本包；
- ``registry``、``manager``、``loader`` 同样收敛到本包：扩展发现与加载治理
  只在 Gateway 进程与母仓之间共享一份实现，``jiuwenswarm.extensions`` 的
  同名导出指向本包；
- ``sdk``、``types``、``callback_compat`` 同样是本包**唯一实现**（均为无状态
  基类/数据类/纯函数），``jiuwenswarm.extensions.sdk`` 只保留惰性转发 shim，
  第三方扩展的 ``from jiuwenswarm.extensions.sdk import ...`` 仍可用；
- 默认扩展发现目录为 ``gateway/extensions``；过渡期仍兼容发现母仓
  ``jiuwenswarm/extensions`` 下尚未迁移的 ``video_duplex``（其执行侧依赖
  AgentServer 会话历史，待按宿主/执行拆分后再迁）。

没有本文件时 setuptools.packages.find 不会把 gateway/extensions 当作包，
wheel 中该目录 0 个文件——部署路径（pip install wheel 后启动）加载 SSH
协议时即 ModuleNotFoundError。B2 合入版（dd67687ba）遗漏了此文件，本
提交补齐（与 !8044 最终版 9ca1d5f3e 一致，该 force-push 评审未及合入）。
"""
