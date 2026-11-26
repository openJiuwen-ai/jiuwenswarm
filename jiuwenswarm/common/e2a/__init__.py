# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""E2A 兼容层（仅保留未随协议迁出的本仓实现）。

E2A wire 契约（常量/模型/规范化/适配/线编解码/ACP 辅助）的唯一真源是
``e2a`` 协议包，本仓调用点直接 ``from e2a.* import ...``。

出站的 ``request_wire`` 将本地请求枚举转为字符串；入站的 ``agent_compat``
返回本仓 AgentRequest，并保留 Gateway 顶层 ``traceparent` 优先级。

协议说明（中英）：仓库 ``docs/zh/E2A-protocol.md``、``docs/en/E2A-protocol.md``。
"""
