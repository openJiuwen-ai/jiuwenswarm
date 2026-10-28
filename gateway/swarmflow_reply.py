# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.swarmflow_reply 参数契约。

真人回复 swarmflow human_session 轮次的入站参数结构。回复经标准三层路由链
（TUI 空壳 ack → message_handler 转发 → process_message 分派）到达
``handle_swarmflow_reply``，由其构造 ``HumanAgentMessage(target="swarmflow:<run_id>:<corr>")``
交 ``TeamManager.interact`` 走 agent-core 薄路由。
"""

from typing import TypedDict


class SwarmflowReplyParams(TypedDict, total=False):
    """chat.swarmflow_reply 参数契约（TypedDict，供类型标注与文档）。"""

    session_id: str
    """团队 session_id（handler 据此定位 team runtime）。"""

    team_name: str
    """团队名。仅日志/调试用，handler 不消费——``TeamManager.interact`` 内部
    用 ``get_active_team_name(session_id)`` 解析。"""

    run_id: str
    """workflow run id。构造 run-scoped reply topic，使并发 run 互不串扰。"""

    correlation_id: str
    """人机轮次关联号 ``{phase}:{label}:{turn}``，跨 resume 稳定。"""

    answer: str
    """真人原始回复正文。"""
