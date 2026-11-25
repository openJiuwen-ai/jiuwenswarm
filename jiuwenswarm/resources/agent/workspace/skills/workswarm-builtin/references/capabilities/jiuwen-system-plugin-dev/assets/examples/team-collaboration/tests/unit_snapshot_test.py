"""Unit tests: pure derivations of the snapshot contract (§2, §4)."""

from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from team_collab_viz import snapshot as snap  # noqa: E402

TEAM = {
    "team_name": "t1",
    "display_name": "团队一",
    "leader_member_name": "leader",
    "desc": None,
    "created": 1000,
}

MEMBERS = [
    {
        "member_name": "qa",
        "display_name": "验收专家",
        "desc": "验收",
        "role": "teammate",
        "status": "ready",
        "execution_status": None,
        "mode": "build_mode",
        "cli_agent": None,
    },
    {
        "member_name": "leader",
        "display_name": "队长甲",
        "desc": None,
        "role": "leader",
        "status": "busy",
        "execution_status": "running",
        "mode": "build_mode",
        "cli_agent": None,
    },
    {
        "member_name": "dev",
        "display_name": "开发乙",
        "desc": "后端",
        "role": "teammate",
        "status": "ready",
        "execution_status": "idle",
        "mode": "build_mode",
        "cli_agent": None,
    },
]

TASKS = [
    {"task_id": "a", "title": "契约", "content": "内容", "status": "completed", "assignee": "leader",
     "updated_at": 10},
    {"task_id": "b", "title": "后端", "content": "内容", "status": "in_progress", "assignee": "dev",
     "updated_at": 20},
    {"task_id": "c", "title": "前端", "content": "内容", "status": "in_progress", "assignee": "dev",
     "updated_at": 30},
    {"task_id": "d", "title": "验收", "content": "内容", "status": "pending", "assignee": None,
     "updated_at": None},
]

DEPS = {
    "a": [],
    "b": [{"task_id": "b", "depends_on_task_id": "a", "resolved": True}],
    "c": [{"task_id": "c", "depends_on_task_id": "a", "resolved": True}],
    "d": [
        {"task_id": "d", "depends_on_task_id": "b", "resolved": True},
        {"task_id": "d", "depends_on_task_id": "c", "resolved": False},
    ],
}


def build(**overrides):
    kwargs = {
        "scope_kind": "session",
        "scope_key": "s1",
        "team": TEAM,
        "members": MEMBERS,
        "tasks": TASKS,
        "dependencies": DEPS,
        "session_map_source": "event",
        "last_event_at": 5000,
        "last_success_at": 6000,
        "stale": False,
        "stale_reason": None,
        "lifecycle_event": "team_created",
        "has_lifecycle_fact": True,
    }
    kwargs.update(overrides)
    return snap.build_snapshot(**kwargs)


def test_progress_ignores_cancelled_in_denominator():
    result = snap.progress_of(["completed", "cancelled", "in_progress", "planning", "in_review", "blocked",
                              "pending"])
    assert result["total"] == 7
    assert result["completed"] == 1
    assert result["cancelled"] == 1
    assert result["in_progress"] == 2  # in_progress + planning
    assert result["in_review"] == 1
    assert result["blocked"] == 1
    assert result["pending"] == 1
    assert result["percent"] == round(1 / 6 * 100, 1)


def test_progress_percent_is_null_without_denominator():
    assert snap.progress_of([])["percent"] is None
    assert snap.progress_of(["cancelled"])["percent"] is None


def test_dependency_inversion_and_layers():
    graph, per_task = snap.build_graph(
        [snap.to_task_dict(task) for task in TASKS],
        snap.to_dependency_map(DEPS),
    )
    assert [(edge["from"], edge["to"]) for edge in graph["edges"]] == [("a", "b"), ("a", "c"), ("b", "d"),
                                                                       ("c", "d")]
    assert per_task["a"]["blocks"] == ["b", "c"]
    assert per_task["d"]["blocked_by"] == ["b", "c"]
    assert per_task["d"]["unresolved_blocked_by"] == ["c"]
    assert per_task["a"]["depth"] == 0
    assert per_task["d"]["depth"] == 2
    assert graph["layers"] == [["a"], ["b", "c"], ["d"]]
    assert graph["node_order"] == ["a", "b", "c", "d"]
    assert graph["has_cycle"] is False


def test_flags_cover_root_branch_join_isolated():
    _, per_task = snap.build_graph(
        [snap.to_task_dict(task) for task in TASKS],
        snap.to_dependency_map(DEPS),
    )
    assert per_task["a"]["flags"] == {
        "is_root": True,
        "is_join": False,
        "is_parallel_branch": True,
        "is_isolated": False,
    }
    assert per_task["d"]["flags"]["is_join"] is True
    assert per_task["d"]["flags"]["is_parallel_branch"] is False
    assert per_task["b"]["flags"]["is_root"] is False


def test_single_task_is_isolated():
    tasks = [snap.to_task_dict({"task_id": "solo", "title": "孤立", "content": "", "status": "pending",
                                "assignee": None, "updated_at": None})]
    graph, per_task = snap.build_graph(tasks, {})
    assert per_task["solo"]["flags"]["is_isolated"] is True
    assert graph["layers"] == [["solo"]]


def test_cycle_is_flagged_not_raised():
    tasks = [
        snap.to_task_dict({"task_id": "x", "title": "x", "content": "", "status": "pending", "assignee": None,
                           "updated_at": None}),
        snap.to_task_dict({"task_id": "y", "title": "y", "content": "", "status": "pending", "assignee": None,
                           "updated_at": None}),
    ]
    graph, per_task = snap.build_graph(tasks, {"x": [("y", True)], "y": [("x", True)]})
    assert graph["has_cycle"] is True
    assert sorted(graph["node_order"]) == ["x", "y"]
    assert per_task["x"]["depth"] >= 0


def test_leader_comes_from_leader_member_name_not_role():
    """§2.4.0: real host data reports ``role == "teammate"`` for everyone."""
    all_teammates = [dict(member, role="teammate") for member in MEMBERS]
    result = build(members=all_teammates)
    captains = [member for member in result["members"] if member["is_leader"]]
    assert [member["member_name"] for member in captains] == ["leader"]
    assert captains[0]["role_group"] == "captain"
    assert captains[0]["role"] == "teammate"          # role stays what the host said
    assert captains[0]["role_label"] == "成员"
    assert result["members"][0]["member_name"] == "leader"   # sorted first
    assert [member["role_group"] for member in result["members"]] == ["captain", "member", "member"]


def test_leader_falls_back_to_role_when_name_matches_nobody():
    """§2.4.0 fallback: leader exited / empty name -> use ``role == "leader"``."""
    exited = dict(TEAM, leader_member_name="ghost")
    result = build(team=exited)
    captains = [member["member_name"] for member in result["members"] if member["is_leader"]]
    assert captains == ["leader"]

    nameless = dict(TEAM, leader_member_name="")
    result = build(team=nameless)
    assert [member["member_name"] for member in result["members"] if member["is_leader"]] == ["leader"]


def test_no_captain_when_neither_signal_exists():
    """§2.4.0: no match and no ``role == "leader"`` -> all plain members (no guessing)."""
    all_teammates = [dict(member, role="teammate") for member in MEMBERS]
    result = build(members=all_teammates, team=dict(TEAM, leader_member_name="ghost"))
    assert all(member["is_leader"] is False for member in result["members"])
    assert {member["role_group"] for member in result["members"]} == {"member"}


def test_leader_member_name_wins_over_role_conflict():
    """§2.4.0: when both signals hit different members, ``leader_member_name`` wins.

    Exactly one captain must remain, otherwise the UI would show two captain
    partitions. ``role``/``role_label`` keep the host's raw values.
    """
    result = build(team=dict(TEAM, leader_member_name="dev"))
    captains = [member["member_name"] for member in result["members"] if member["is_leader"]]
    assert captains == ["dev"]
    by_name = {member["member_name"]: member for member in result["members"]}
    assert by_name["leader"]["role"] == "leader"
    assert by_name["leader"]["role_label"] == "队长"
    assert by_name["leader"]["role_group"] == "member"
    assert sum(1 for member in result["members"] if member["is_leader"]) == 1


def test_multiple_role_leaders_pick_lexicographically_smallest():
    """§2.4.0 fallback determinism: several ``role == "leader"`` -> smallest name only."""
    members = [
        dict(member, role="leader" if member["member_name"] in ("qa", "leader") else "teammate")
        for member in MEMBERS
    ]
    result = build(members=members, team=dict(TEAM, leader_member_name=""))
    captains = [member["member_name"] for member in result["members"] if member["is_leader"]]
    assert captains == ["leader"]          # "leader" < "qa" lexicographically
    assert sum(1 for member in result["members"] if member["is_leader"]) == 1
    by_name = {member["member_name"]: member for member in result["members"]}
    assert by_name["qa"]["role_group"] == "member"
    assert by_name["qa"]["role"] == "leader"   # raw role untouched

    # name pointing at a member that left -> fallback still yields exactly one captain
    result = build(members=members, team=dict(TEAM, leader_member_name="ghost"))
    assert [m["member_name"] for m in result["members"] if m["is_leader"]] == ["leader"]


def test_members_sorted_leader_first_then_name():
    result = build()
    assert [member["member_name"] for member in result["members"]] == ["leader", "dev", "qa"]
    assert [member["role_group"] for member in result["members"]] == ["captain", "member", "member"]
    assert result["members"][0]["is_leader"] is True


def test_member_labels_and_role_label_are_present():
    result = build()
    by_name = {member["member_name"]: member for member in result["members"]}
    assert by_name["leader"]["status_label"] == "执行中"
    assert by_name["leader"]["execution_label"] == "运行中"
    assert by_name["leader"]["role_label"] == "队长"
    assert by_name["dev"]["role_label"] == "成员"
    assert by_name["qa"]["execution_label"] is None
    assert "unknown_enum" not in by_name["qa"]


def test_unknown_enum_falls_back_to_raw_value():
    members = [dict(MEMBERS[0], status="weird_status", role="spy")]
    result = build(members=members, tasks=[])
    member = result["members"][0]
    assert member["status_label"] == "weird_status"
    assert member["role_label"] == "spy"
    assert member["unknown_enum"] is True


def test_current_task_picks_newest_updated_at():
    result = build()
    by_name = {member["member_name"]: member for member in result["members"]}
    dev = by_name["dev"]
    assert dev["current_task"]["task_id"] == "c"
    assert dev["current_task"]["status_label"] == "进行中"
    assert dev["active_task_count"] == 2
    assert dev["task_progress"]["total"] == 2
    assert dev["task_progress"]["percent"] == 0.0
    assert by_name["qa"]["current_task"] is None
    assert by_name["qa"]["active_task_count"] == 0


def test_current_task_tie_breaks_on_task_id():
    tasks = [
        {"task_id": "z2", "title": "z", "content": "", "status": "in_progress", "assignee": "dev",
         "updated_at": 5},
        {"task_id": "z1", "title": "z", "content": "", "status": "in_progress", "assignee": "dev",
         "updated_at": 5},
    ]
    result = build(tasks=tasks, dependencies={"z1": [], "z2": []})
    dev = {member["member_name"]: member for member in result["members"]}["dev"]
    assert dev["current_task"]["task_id"] == "z1"


def test_task_blocks_carry_assignee_display_name():
    result = build()
    by_id = {task["task_id"]: task for task in result["tasks"]}
    assert by_id["b"]["assignee"] == {"member_name": "dev", "display_name": "开发乙"}
    assert by_id["d"]["assignee"] is None
    assert by_id["a"]["status_label"] == "已完成"


def test_assignee_missing_from_members_falls_back_to_null_display_name():
    tasks = [{"task_id": "x", "title": "x", "content": "", "status": "pending", "assignee": "ghost",
              "updated_at": None}]
    result = build(tasks=tasks, dependencies={"x": []})
    assert result["tasks"][0]["assignee"] == {"member_name": "ghost", "display_name": None}


def test_team_status_running_when_task_open():
    result = build()
    assert result["team"]["status"] == "running"
    assert result["team"]["status_source"] == "event"
    assert result["team"]["status_label"] == "运行中"
    assert result["team"]["progress"]["total"] == 4


def test_team_status_history_without_lifecycle_fact():
    result = build(has_lifecycle_fact=False, lifecycle_event=None)
    assert result["team"]["status"] == "history"
    assert result["team"]["status_source"] == "history"
    assert result["team"]["status_label"] == "历史记录"


def test_team_status_cleaned_wins():
    result = build(lifecycle_event="team_cleaned")
    assert result["team"]["status"] == "cleaned"
    assert result["team"]["status_label"] == "已结束"


def test_team_status_standby_before_derived_completed():
    settled = [
        dict(MEMBERS[0], status="paused"),
        dict(MEMBERS[1], status="stopped"),
        dict(MEMBERS[2], status="shut_down"),
    ]
    done = [dict(task, status="completed") for task in TASKS]
    result = build(members=settled, tasks=done, lifecycle_event="team_standby")
    assert result["team"]["status"] == "standby"
    assert result["team"]["status_source"] == "event"


def test_team_status_derived_completed():
    settled = [
        dict(MEMBERS[0], status="paused"),
        dict(MEMBERS[1], status="dormant"),
        dict(MEMBERS[2], status="stopped"),
    ]
    done = [dict(task, status="completed") for task in TASKS]
    result = build(members=settled, tasks=done)
    assert result["team"]["status"] == "completed"
    assert result["team"]["status_source"] == "derived"
    assert result["team"]["progress"]["percent"] == 100.0


def test_team_status_not_derived_while_a_member_is_busy():
    done = [dict(task, status="completed") for task in TASKS]
    result = build(tasks=done)
    assert result["team"]["status"] == "running"


def test_empty_snapshot_never_fabricates_team():
    result = snap.empty_snapshot(scope_kind="session", scope_key="s1")
    assert result["schema_version"] == "1.1"
    assert result["team"] is None
    assert result["members"] == []
    assert result["tasks"] == []
    assert result["graph"] == {"edges": [], "layers": [], "node_order": [], "has_cycle": False}
    assert result["limits"] == {"members_total": 0, "tasks_total": 0}
    assert result["stale"] is True
    assert result["stale_reason"] == "no_session_map"
    assert result["availability"]["session_map_source"] == "none"
    assert result["availability"]["team_readable"] is False


def test_scope_and_availability_pass_through():
    result = build(stale=True, stale_reason="ops_error", last_event_at=None, last_success_at=None)
    assert result["scope"] == {"kind": "session", "key": "s1"}
    assert result["stale"] is True
    assert result["stale_reason"] == "ops_error"
    assert result["availability"]["last_event_at"] is None
    assert result["availability"]["team_readable"] is True


def test_snapshot_cache_revisions_and_waiters():
    cache = snap.SnapshotCache()
    assert cache.publish("k", {"value": 1}) == 1
    assert cache.publish("k", {"value": 2}) == 2
    assert cache.revision_of("k") == 2
    stored, revision = cache.get("k")
    assert revision == 2
    assert stored["revision"] == 2
    assert cache.wait_change("k", 2, 0.05) is None
    assert cache.scope_count() == 1
    assert cache.summary()["scopes"] == 1


def test_snapshot_cache_ensure_is_stable():
    cache = snap.SnapshotCache()
    first = cache.ensure("k", lambda: snap.empty_snapshot(scope_kind="global", scope_key="global"))
    second = cache.ensure("k", lambda: snap.empty_snapshot(scope_kind="global", scope_key="global"))
    assert first is second
    assert first["revision"] == 1
