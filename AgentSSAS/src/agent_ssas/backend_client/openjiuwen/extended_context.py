# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""ExtendedSecurityCheckContext - 扩展 SecurityCheckContext,新增 ID 字段。"""

from dataclasses import dataclass

from openjiuwen.harness.rails.security import SecurityCheckContext


@dataclass
class ExtendedSecurityCheckContext(SecurityCheckContext):
    """扩展 SecurityCheckContext,新增 ID 字段。纯增量,有缺省值。

    agent-core 的原始 SecurityCheckContext 保持不变,
    此子类仅在 AgentSecurity/AgentSSAS 仓中新增 ID 字段。
    """

    # 新增 ID 字段(int 类型字段缺省值为 -1,str 类型字段缺省值为空字符串)
    # 缺省值为 -1,表示当前事件不具备此序号字段。首个有效值为 0,从 0 开始计数。
    interaction_seq: int = -1
    session_id: str = ""
    agent_id: str = ""
    trace_id: str = ""
    context_id: str = ""
    conversation_id: str = ""
    tool_call_id: str = ""
    tool_name: str = ""
    llm_call_seq: int = -1
    tool_call_seq: int = -1
    subsession_id: str = ""
