# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""主 Agent / leader 专属工具名集合（子 Agent 装配期与执行期共用）。

无 openjiuwen 运行时依赖，便于装配期过滤与单测复用。
"""

from __future__ import annotations

#: 主 Agent 专属工具集合（0708 ``EXCLUDED_TOOLS_SPAWN`` 平移）。
#: GP inject 的 ``tools=`` 由 ``excluded_spawn_tools`` 过滤；``SubagentPermissionRail`` 作执行期深防御。
EXCLUDED_TOOLS_SPAWN = frozenset({
    "spawn_subagent",
    "send_file_to_user",
    # 主 Agent 级调度与消息（子 Agent 不应触发）
    "office_claw_dispatch_agent_task",
    "office_claw_post_message",
    "office_claw_get_pending_mentions",
    "office_claw_ack_mentions",
    "office_claw_get_thread_context",
    "office_claw_list_threads",
    "office_claw_cross_post_message",
    "office_claw_register_pr_tracking",
    "office_claw_multi_mention",
    # 主 Agent 级计划任务
    "office_claw_list_scheduled_tasks",
    "office_claw_list_schedule_templates",
    "office_claw_preview_scheduled_task",
    "office_claw_register_scheduled_task",
    "office_claw_set_scheduled_task_enabled",
    "office_claw_remove_scheduled_task",
    "office_claw_update_scheduled_task",
    # 主 Agent 级记忆与反思
    "office_claw_retain_memory_callback",
    "office_claw_search_evidence",
    "office_claw_reflect",
    # 主 Agent 级会话链追踪
    "office_claw_list_session_chain",
    "office_claw_read_session_events",
    "office_claw_read_session_digest",
    "office_claw_read_invocation_detail",
    # 主 Agent 级技能管理
    "office_claw_list_skills",
})

__all__ = ["EXCLUDED_TOOLS_SPAWN"]
