"""team.snapshot 滞留成员活态读时归一测试。

背景：desktop 退出 = 后端被 taskkill /T /F 强杀，team.db 成员 busy/starting/
restarting 永久滞留且不自愈；重启后打开历史会话走 DB 纯读路径，状态原样
透传会让面板永远显示「工作中」。normalize_stale_member_statuses 在无活跃
运行时时把滞留活态归一为 paused（现场保留，下次提问 recover_team 重拉起）。
"""
from jiuwenswarm.server.agent_ws_server import normalize_stale_member_statuses


def _payload(*members: dict) -> dict:
    return {"members": list(members), "tasks": [], "team_id": "t1"}


class TestNormalizeStaleMemberStatuses:
    def test_busy_with_open_task_normalized_to_paused(self):
        """busy + 名下有未终态任务 = 确有挂起现场 → 已暂停。"""
        payload = _payload(
            {"member_id": "m1", "status": "busy", "execution_status": "running"},
        )
        payload["tasks"] = [{"task_id": "t1", "status": "in_progress", "assignee": "m1"}]
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0]["status"] == "paused"
        assert payload["members"][0]["execution_status"] == "idle"

    def test_busy_with_only_terminal_tasks_normalized_to_ready(self):
        """busy 但任务全已完成（干完了没来得及回 ready 就被杀）→ 空闲，不标已暂停。"""
        payload = _payload(
            {"member_id": "m1", "status": "busy"},
        )
        payload["tasks"] = [{"task_id": "t1", "status": "completed", "assignee": "m1"}]
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0]["status"] == "ready"

    def test_busy_without_any_task_normalized_to_ready(self):
        payload = _payload(
            {"member_id": "m1", "status": "busy"},
        )
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0]["status"] == "ready"

    def test_open_task_of_other_member_does_not_pause_this_one(self):
        payload = _payload(
            {"member_id": "m1", "status": "busy"},
            {"member_id": "m2", "status": "busy"},
        )
        payload["tasks"] = [{"task_id": "t1", "status": "in_progress", "assignee": "m2"}]
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0]["status"] == "ready"
        assert payload["members"][1]["status"] == "paused"

    def test_spawning_statuses_normalized_to_ready_not_paused(self):
        """starting/restarting 是拉起途中被杀、没有工作现场——归一为 ready（空闲），
        不标已暂停；且不能原样透传（前端会把 starting/restarting 映射成 busy）。"""
        payload = _payload(
            {"member_id": "m1", "status": "starting", "execution_status": "starting"},
            {"member_id": "m2", "status": "restarting"},
        )
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0]["status"] == "ready"
        assert payload["members"][0]["execution_status"] == "idle"
        assert payload["members"][1]["status"] == "ready"

    def test_active_runtime_untouched(self):
        payload = _payload({"member_id": "m1", "status": "busy", "execution_status": "running"})
        normalize_stale_member_statuses(payload, has_runtime=True)
        assert payload["members"][0]["status"] == "busy"
        assert payload["members"][0]["execution_status"] == "running"

    def test_settled_statuses_untouched(self):
        payload = _payload(
            {"member_id": "m1", "status": "ready"},
            {"member_id": "m2", "status": "paused"},
            {"member_id": "m3", "status": "stopped"},
            {"member_id": "m4", "status": "shut_down"},
            {"member_id": "m5", "status": "error"},
        )
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert [m["status"] for m in payload["members"]] == [
            "ready", "paused", "stopped", "shut_down", "error",
        ]

    def test_idle_execution_untouched(self):
        payload = _payload({"member_id": "m1", "status": "ready", "execution_status": "idle"})
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0]["execution_status"] == "idle"

    def test_malformed_payload_safe(self):
        assert normalize_stale_member_statuses({}, False) == {}
        assert normalize_stale_member_statuses({"members": "oops"}, False) == {"members": "oops"}
        payload = _payload("not-a-dict", {"member_id": "m1", "status": "busy"})
        normalize_stale_member_statuses(payload, has_runtime=False)
        assert payload["members"][0] == "not-a-dict"
        assert payload["members"][1]["status"] == "ready"  # 无挂起任务 → ready
