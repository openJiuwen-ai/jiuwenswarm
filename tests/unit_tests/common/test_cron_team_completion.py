from jiuwenswarm.common.cron_team_completion import (
    apply_cron_team_round_event,
    cron_team_round_should_end,
    is_cron_leader_placeholder_text,
    new_cron_team_round_state,
)


class TestCronTeamCompletionSignals:
    @staticmethod
    def test_placeholder_detection():
        assert is_cron_leader_placeholder_text("最终报告即将生成，请稍候。")
        assert not is_cron_leader_placeholder_text("## 审查完成\n\n最终建议: approve")

    @staticmethod
    def test_swarmflow_end_requires_workflow_and_leader_final():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {"event_type": "workflow.updated", "workflow": {"status": "completed"}},
        )
        assert not cron_team_round_should_end(state)
        apply_cron_team_round_event(
            state,
            {"event_type": "chat.final", "content": "## 审查完成", "rid": 1},
        )
        assert cron_team_round_should_end(state)

    @staticmethod
    def test_harness_end_on_leader_final_without_team_completed():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {"event_type": "chat.final", "content": "GLM vs DeepSeek 对比完成", "rid": 2},
        )
        assert cron_team_round_should_end(state)

    @staticmethod
    def test_team_completed_requires_result_text():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(state, {"event_type": "team.completed"})
        assert not cron_team_round_should_end(state)

    @staticmethod
    def test_follow_up_style_round_end():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {"event_type": "chat.final", "content": "任务完成，汇总如下。", "rid": 3},
        )
        assert cron_team_round_should_end(state)

    @staticmethod
    def test_swarmflow_ignores_interim_leader_final_while_running():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "workflow.updated",
                "workflow": {"status": "running", "summary": ""},
            },
        )
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "content": "Phase 1 — Parallel Review 进行中，三位审查者已启动。",
                "rid": 4,
            },
        )
        assert state.get("workflow_started") is True
        assert not cron_team_round_should_end(state)

    @staticmethod
    def test_placeholder_final_does_not_end_round():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {"event_type": "chat.final", "content": "最终报告即将生成，请稍候。", "rid": 4},
        )
        assert not cron_team_round_should_end(state)

    @staticmethod
    def test_chunk_complete_ignored_while_delegated_tasks_open():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "team.task",
                "event": {"type": "team.task.created", "task_id": "glm-research"},
            },
        )
        assert not cron_team_round_should_end(state, chunk_complete=True)

    @staticmethod
    def test_chunk_complete_honored_after_harness_leader_final():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "content": "## GLM vs DeepSeek 对比完成",
                "rid": 1,
            },
        )
        assert cron_team_round_should_end(state, chunk_complete=True)

    @staticmethod
    def test_processing_status_complete_does_not_end_delegated_round():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "team.task",
                "event": {"type": "team.task.created", "task_id": "glm-research"},
            },
        )
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.processing_status",
                "is_complete": True,
            },
        )
        assert not state.get("team_round_completed")
        assert not cron_team_round_should_end(state)
        assert not cron_team_round_should_end(state, chunk_complete=True)

    @staticmethod
    def test_leader_final_before_task_created_is_not_terminal():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "content": "已分派 glm-analyst 与 deepseek-analyst 并行调研，请稍候。",
                "rid": 1,
            },
        )
        assert state.get("leader_final_seen") is True
        # Harness-only completion would fire here; task.created must clear the latch.
        assert cron_team_round_should_end(state)

        apply_cron_team_round_event(
            state,
            {
                "event_type": "team.task",
                "event": {"type": "team.task.created", "task_id": "glm-research"},
            },
        )
        assert state.get("leader_final_seen") is False
        assert not cron_team_round_should_end(state)

        apply_cron_team_round_event(
            state,
            {
                "event_type": "team.task",
                "event": {"type": "team.task.completed", "task_id": "glm-research"},
            },
        )
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "content": "## GLM vs DeepSeek 最新进展对比",
                "rid": 2,
            },
        )
        assert cron_team_round_should_end(state)

    @staticmethod
    def test_harness_ignores_interim_leader_final_while_tasks_open():
        state = new_cron_team_round_state()
        for task_id in ("research-glm", "research-deepseek", "research-qwen"):
            apply_cron_team_round_event(
                state,
                {
                    "event_type": "team.task",
                    "event": {"type": "team.task.created", "task_id": task_id},
                },
            )
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "content": (
                    "三路调研已并行启动，三位调研专家正在各自方向搜索。"
                    "等待他们完成调研后我会汇总整合为最终报告。"
                ),
                "rid": 1,
            },
        )
        assert not cron_team_round_should_end(state)

        for task_id in ("research-glm", "research-deepseek", "research-qwen"):
            apply_cron_team_round_event(
                state,
                {
                    "event_type": "team.task",
                    "event": {"type": "team.task.completed", "task_id": task_id},
                },
            )
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "content": "## GLM vs DeepSeek vs Qwen 最新进展对比",
                "rid": 2,
            },
        )
        assert cron_team_round_should_end(state)

    @staticmethod
    def test_teammate_final_does_not_count_as_leader_result():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "role": "teammate",
                "member_name": "comparison-analyst",
                "content": "明白，静候佳音。",
                "rid": 1,
            },
        )
        assert not state.get("leader_final_seen")
        assert not cron_team_round_should_end(state)

    @staticmethod
    def test_active_member_blocks_harness_end():
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "team.member",
                "event": {
                    "type": "team.member.status_changed",
                    "member_id": "deepseek-researcher",
                    "new_status": "busy",
                },
            },
        )
        apply_cron_team_round_event(
            state,
            {
                "event_type": "chat.final",
                "role": "assistant",
                "content": "## GLM vs DeepSeek 技术路线与商业策略对比",
                "rid": 2,
            },
        )
        assert not cron_team_round_should_end(state)

    @staticmethod
    def test_team_error_records_reason_and_clears_round():
        """team.error 与 chat.error 同为终端失败信号。

        team.error 由团队运行时直接抛出，不保证经 gateway 归一化成 chat.error。
        漏认会让轮次状态里不留任何错误痕迹，cron 只能兜底成无细节的
        「定时任务未产生有效报告」，后端真实报错因此丢失。
        """
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {
                "event_type": "team.task",
                "event": {"type": "task.created", "task_id": "t-1"},
            },
        )
        assert state["open_team_tasks"]

        apply_cron_team_round_event(
            state,
            {"event_type": "team.error", "error": "模型调用失败: 429 rate limited"},
        )
        assert state["leader_text"] == "模型调用失败: 429 rate limited"
        assert state["leader_final_seen"] is True
        assert state["open_team_tasks"] == {}
        assert state["active_team_members"] == {}

    @staticmethod
    def test_team_error_accepts_message_field():
        """team.error 的报错文本可能只在 message 字段（与 CLI handle_error 一致）。"""
        state = new_cron_team_round_state()
        apply_cron_team_round_event(
            state,
            {"event_type": "team.error", "message": "round aborted"},
        )
        assert state["leader_text"] == "round aborted"
        assert state["leader_final_seen"] is True
