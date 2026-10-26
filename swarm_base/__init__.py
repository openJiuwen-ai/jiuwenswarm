# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway 仓共享底座（原 ``jiuwenswarm/common`` 的双侧共用部分真源）。

与 ``gateway/`` 通道包并列的顶层包，承载 gateway 进程与 server/agents
双侧共用的领域逻辑与契约（A 线等价面）。约束（R-C）：本包**禁止**
import ``gateway.*``（通道逻辑）——纯度由 ``scripts/check_layering.py``
的 R-C 规则守卫。母仓 ``jiuwenswarm/common/X`` 对应件为转发 shim，
母仓侧 import 路径不变。
"""
