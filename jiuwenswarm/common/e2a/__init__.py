# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""E2A 兼容层（仅保留未随协议迁出的本仓实现）。

E2A wire 契约（常量/模型/规范化/适配/线编解码/ACP 辅助）的唯一真源是
``e2a`` 协议包，本仓调用点已直接 ``from e2a.* import ...``。

本包只保留两处依赖本仓类型的实现：
- ``agent_compat.e2a_to_agent_request``：将方法字符串解析为本仓
  ``schema.message.ReqMethod``，并产出 ``schema.agent.AgentRequest``。
- ``gateway_normalize``：在协议实现之上包装 Team/组织 chat.delta 身份字段。

协议说明（中英）：仓库 ``docs/zh/E2A-protocol.md``、``docs/en/E2A-protocol.md``。
"""
