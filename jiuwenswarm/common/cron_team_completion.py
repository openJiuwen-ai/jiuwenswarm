# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""转发别名（M1' 过渡形态）：真源已迁 ``swarm_base.cron_team_completion``。"""

from swarm_base.cron_team_completion import (  # noqa: F401
    CRON_LEADER_PLACEHOLDER_MARKERS,
    _apply_team_member_event,
    _apply_team_task_event,
    _cron_solo_harness_end_pending,
    _drain_cron_delegation_grace_events,
    _extract_workflow_summary,
    _harness_round_can_end,
    _is_cron_leader_final_event,
    apply_cron_team_round_event,
    cron_team_round_has_active_members,
    cron_team_round_has_open_tasks,
    cron_team_round_has_result_text,
    cron_team_round_should_end,
    is_cron_leader_placeholder_text,
    new_cron_team_round_state,
)
