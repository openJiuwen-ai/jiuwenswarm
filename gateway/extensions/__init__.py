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
- ``sdk``、``types``、``callback_compat`` 同样是本包**唯一实现**（均为无状态
  基类/数据类/纯函数），``jiuwenswarm.extensions.sdk`` 只保留惰性转发 shim，
  第三方扩展的 ``from jiuwenswarm.extensions.sdk import ...`` 仍可用；
- ``registry`` 框架保留母仓与 gateway 各一份：``ExtensionRegistry`` 是进程内
  单例并会安装全局 crypto 钩子，双份类定义可避免同进程内单例互相抢占；
- ``jiuwenswarm.extensions`` 仍作为扩展发现/加载入口（manager/loader 暂留母仓）。

没有本文件时 setuptools.packages.find 不会把 gateway/extensions 当作包，
wheel 中该目录 0 个文件——部署路径（pip install wheel 后启动）加载 SSH
协议时即 ModuleNotFoundError。B2 合入版（dd67687ba）遗漏了此文件，本
提交补齐（与 !8044 最终版 9ca1d5f3e 一致，该 force-push 评审未及合入）。
"""
