# Copyright (c) Huawei Technologies Co., Ltd. 2025. All rights reserved.
"""E2A（Everything-to-Agent）：统一信封；ACP / A2A 等经转换进入 E2A，并由 provenance 记录出处。

E2A wire 契约（常量/模型/规范化/适配/线编解码/ACP 辅助）的 source of truth 为
``gateway_protocol.e2a``，仓内 import 直指协议包。本包仅保留 ``agent_compat``：
``e2a_to_agent_request`` 依赖本仓 ``schema.message.ReqMethod`` 实例构造
（ReqMethod 耦合 Mode 产品逻辑，尚未协议化），为本仓实现，见
``jiuwenswarm/common/e2a/agent_compat.py``。

协议说明（中英）：``docs/zh/E2A-protocol.md``、``docs/en/E2A-protocol.md``。
"""
