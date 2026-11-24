# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""E2A 兼容层（仅保留未随协议迁出的本仓实现）。

E2A wire 契约（常量/模型/规范化/适配/线编解码/ACP 辅助）的唯一真源是
``e2a`` 协议包，本仓调用点直接 ``from e2a.* import ...``。

本包只保留 ``agent_compat``：在 ``e2a.agent_compat`` 之上应用 Gateway
顶层 ``traceparent`` 优先级约定。

协议说明（中英）：仓库 ``docs/zh/E2A-protocol.md``、``docs/en/E2A-protocol.md``。
"""
