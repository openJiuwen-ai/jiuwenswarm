# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-owned extension assets: SSH auth suite and application plugin host.

从 ``jiuwenswarm/extensions`` 吸收的 gateway 专属件（B2）：auth 认证套件
（SSH 协议通道宿主）与 application_host（应用插件 HTTP/WS 宿主）。共享的
hook_event / hooks_context 已下沉 ``jiuwenswarm.common``。原位置保留兼容
shim；加载治理见 ``extensions/manager.py`` 的清单化配置。

没有本文件时 setuptools.packages.find 不会把 gateway/extensions 当作包，
wheel 中该目录 0 个文件——部署路径（pip install wheel 后启动）加载 SSH
协议时即 ModuleNotFoundError。B2 合入版（dd67687ba）遗漏了此文件，本
提交补齐（与 !8044 最终版 9ca1d5f3e 一致，该 force-push 评审未及合入）。
"""
