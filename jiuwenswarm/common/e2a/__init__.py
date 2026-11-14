# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""E2A 兼容层（仅保留未随协议迁出的本仓实现）。

E2A wire 契约（常量/信封/适配/线编解码/ACP 辅助）由 ``e2a`` 协议包维护。
请求方法目录及携带该枚举的 Message / AgentRequest 由本仓 schema 维护。

本包只保留两处依赖本仓类型的实现：
- ``agent_compat.e2a_to_agent_request``：产出本仓 ``schema.agent.AgentRequest``。
- ``gateway_normalize``：本地请求方法规范化，以及 Team/组织 chat.delta 身份字段。

协议说明（中英）：仓库 ``docs/zh/E2A-protocol.md``、``docs/en/E2A-protocol.md``。
"""
