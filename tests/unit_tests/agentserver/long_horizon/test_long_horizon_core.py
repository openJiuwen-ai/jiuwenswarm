from __future__ import annotations

import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

import jiuwenswarm.agents.harness.common.long_horizon.core as long_horizon_store
from jiuwenswarm.agents.harness.common.long_horizon.core import (
    ScheduleError,
    append_event,
    build_kick_query,
    draft_long_horizon_task,
    fill_stage_due_dates,
    get_long_horizon_task,
    load_long_horizon_tasks,
    require_brief,
    require_stage_plans,
    upsert_long_horizon_task,
)
from jiuwenswarm.agents.harness.common.long_horizon.models import (
    Anchor,
    LongHorizonStage,
    LongHorizonTask,
)

NOW = datetime(2026, 9, 16, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def make_task() -> LongHorizonTask:
    return draft_long_horizon_task(
        title="年度发布",
        month=12,
        day=20,
        stages=[
            {
                "title": "准备材料",
                "month": 11,
                "day": 20,
                "plan": "整理发布清单并标记负责人。",
            },
            {
                "title": "发布检查",
                "month": 12,
                "day": 20,
                "plan": "逐项核对清单并输出检查结论。",
            },
        ],
        now=NOW,
    )


def test_draft_rejects_missing_stages():
    with pytest.raises(ScheduleError, match="stages"):
        draft_long_horizon_task(
            title="年度发布",
            month=12,
            day=20,
            stages=[],
            now=NOW,
        )


def test_draft_uses_only_explicit_stages_and_timezone_aware_due_dates():
    task = make_task()

    assert [stage.title for stage in task.stages] == ["准备材料", "发布检查"]
    assert all(datetime.fromisoformat(stage.due_at).tzinfo is not None for stage in task.stages)
    assert datetime.fromisoformat(task.stages[0].due_at) == datetime(
        2026, 11, 20, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )


def test_sanitize_task_title_strips_stage_meta_suffix():
    from jiuwenswarm.agents.harness.common.long_horizon.core import (
        sanitize_task_title,
    )

    assert (
        sanitize_task_title("智能体 × 高校合作筹备（五阶段推进）")
        == "智能体 × 高校合作筹备"
    )
    assert sanitize_task_title("年度发布(3阶段)") == "年度发布"
    assert sanitize_task_title("普通标题") == "普通标题"
    task = draft_long_horizon_task(
        title="智能体 × 高校合作筹备（五阶段推进）",
        month=10,
        day=8,
        stages=[
            {
                "title": "内部对齐",
                "due_at": "2026-09-17 09:00",
                "plan": "对齐材料。",
            }
        ],
        now=NOW,
    )
    assert task.title == "智能体 × 高校合作筹备"


def test_offset_days_cannot_replace_an_explicit_stage_date():
    with pytest.raises(ScheduleError, match="stage_date_required"):
        draft_long_horizon_task(
            title="年度发布",
            month=12,
            day=20,
            stages=[
                {
                    "title": "准备材料",
                    "offset_days": -30,
                    "plan": "整理材料。",
                }
            ],
            now=NOW,
        )


def test_explicit_due_at_is_timezone_aware_and_preserves_time():
    task = draft_long_horizon_task(
        title="年度发布",
        month=12,
        day=20,
        stages=[
            {
                "title": "准备材料",
                "due_at": "2026-10-01T14:30:00+08:00",
                "plan": "整理材料。",
            }
        ],
        now=NOW,
    )

    assert task.stages[0].due_at == "2026-10-01T14:30:00+08:00"


def test_naive_due_at_is_bound_to_task_timezone():
    task = draft_long_horizon_task(
        title="年度发布",
        month=12,
        day=20,
        timezone="Asia/Shanghai",
        stages=[
            {
                "title": "准备材料",
                "due_at": "2026-10-01T14:30:00",
                "plan": "整理材料。",
            }
        ],
        now=NOW,
    )
    assert task.stages[0].due_at.endswith("+08:00")
    assert "2026-10-01T14:30:00" in task.stages[0].due_at


def test_invalid_timezone_is_reported_as_schedule_error():
    with pytest.raises(ScheduleError, match="invalid_timezone"):
        draft_long_horizon_task(
            title="年度发布",
            month=12,
            day=20,
            timezone="Mars/Olympus",
            stages=[
                {
                    "title": "准备材料",
                    "month": 11,
                    "day": 20,
                    "plan": "整理材料。",
                }
            ],
            now=NOW,
        )


def test_invalid_anchor_date_is_reported_as_schedule_error():
    with pytest.raises(ScheduleError, match="invalid_anchor_date"):
        draft_long_horizon_task(
            title="年度发布",
            month=12,
            day="not-a-day",
            stages=[
                {
                    "title": "准备材料",
                    "month": 11,
                    "day": 20,
                    "plan": "整理材料。",
                }
            ],
            now=NOW,
        )


@pytest.mark.parametrize(
    ("month", "day"),
    [(13, 1), (2, 30)],
)
def test_invalid_stage_calendar_date_is_rejected(month, day):
    with pytest.raises(ScheduleError, match="invalid_stage_date"):
        draft_long_horizon_task(
            title="年度发布",
            month=12,
            day=20,
            stages=[
                {
                    "title": "准备材料",
                    "month": month,
                    "day": day,
                    "plan": "整理材料。",
                }
            ],
            now=NOW,
        )


def test_cross_year_stage_belongs_to_cycle_before_next_anchor():
    now = datetime(2026, 11, 15, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))

    task = draft_long_horizon_task(
        title="春季发布",
        month=3,
        day=1,
        stages=[
            {
                "title": "冬季准备",
                "month": 12,
                "day": 1,
                "plan": "准备春季发布材料。",
            }
        ],
        now=now,
    )

    assert datetime.fromisoformat(task.stages[0].due_at) == datetime(
        2026, 12, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )


def test_fill_stage_due_dates_rejects_stage_already_in_past():
    task = LongHorizonTask.new(
        title="年度发布",
        month=12,
        day=20,
        timezone="Asia/Shanghai",
    )
    task.stages = [
        LongHorizonStage(
            id="lhs_expired",
            offset_days=-122,
            title="已过期阶段",
            plan="整理材料。",
        )
    ]

    with pytest.raises(ScheduleError, match="stage_in_past"):
        fill_stage_due_dates(task, now=NOW)


def test_same_day_past_clock_time_is_bumped_five_minutes_ahead():
    now = datetime(2026, 9, 17, 14, 52, tzinfo=ZoneInfo("Asia/Shanghai"))
    task = draft_long_horizon_task(
        title="智能体高校合作",
        month=10,
        day=8,
        stages=[
            {
                "title": "内部对齐",
                "month": 9,
                "day": 17,
                "plan": "收口方案一页纸与三份日程草稿。",
            },
            {
                "title": "接待老师",
                "month": 9,
                "day": 22,
                "time": "10:00",
                "plan": "校门口接人并座谈演示。",
            },
        ],
        now=now,
    )

    due = datetime.fromisoformat(task.stages[0].due_at)
    assert due == now + timedelta(minutes=5)
    assert datetime.fromisoformat(task.stages[1].due_at) == datetime(
        2026, 9, 22, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")
    )


def test_stage_specs_honor_named_clock_times():
    now = datetime(2026, 9, 17, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    task = draft_long_horizon_task(
        title="智能体高校合作",
        month=10,
        day=8,
        stages=[
            {
                "title": "接待老师",
                "month": 9,
                "day": 22,
                "time": "10:00",
                "plan": "校门口 10:00 接人。",
            },
            {
                "title": "启动大会",
                "month": 9,
                "day": 25,
                "hour": 14,
                "minute": 0,
                "plan": "主会场 14:00–17:00。",
            },
            {
                "title": "张教授讲座",
                "month": 9,
                "day": 28,
                "time": "19:30",
                "plan": "报告厅讲座。",
            },
            {
                "title": "复盘",
                "month": 10,
                "day": 8,
                "plan": "线上复盘输出纪要。",
            },
        ],
        now=now,
    )

    assert [datetime.fromisoformat(stage.due_at) for stage in task.stages] == [
        datetime(2026, 9, 22, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 9, 25, 14, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 9, 28, 19, 30, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2026, 10, 8, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ]


def test_stage_specs_accept_due_at_with_year_across_new_year():
    now = datetime(2026, 12, 1, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    task = draft_long_horizon_task(
        title="跨年筹备",
        month=2,
        day=1,
        stages=[
            {
                "title": "年底对齐",
                "due_at": "2026-12-20 10:00",
                "plan": "对齐明年计划。",
            },
            {
                "title": "开年启动",
                "due_at": "2027-02-01 09:00",
                "plan": "启动执行。",
            },
        ],
        now=now,
    )
    assert [datetime.fromisoformat(stage.due_at) for stage in task.stages] == [
        datetime(2026, 12, 20, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        datetime(2027, 2, 1, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
    ]


def test_every_stage_requires_plan():
    task = draft_long_horizon_task(
        title="年度发布",
        month=12,
        day=20,
        stages=[{"title": "准备材料", "month": 11, "day": 20, "plan": ""}],
        now=NOW,
    )

    with pytest.raises(ValueError, match="stage_plan_required"):
        require_stage_plans(task)


def test_confirm_brief_is_required():
    with pytest.raises(ValueError, match="brief_required"):
        require_brief(make_task(), "")


def test_brief_and_stage_plans_are_normalized_and_clipped():
    task = make_task()
    task.stages[0].plan = "  整理   材料  "

    assert len(require_brief(task, "目标 " * 500)) == 800
    assert require_stage_plans(task) is None
    assert task.stages[0].plan == "整理 材料"


def test_models_use_long_horizon_ids_and_preserve_status_round_trip():
    task = make_task()
    task.status = "active"
    task.stages[0].status = "due"

    restored = LongHorizonTask.from_dict(task.to_dict())

    assert restored.id.startswith("lhc_")
    assert all(stage.id.startswith("lhs_") for stage in restored.stages)
    assert restored.status == "active"
    assert restored.stages[0].status == "due"
    assert restored.to_dict() == task.to_dict()


def test_task_from_dict_rejects_empty_stages():
    data = make_task().to_dict()
    data["stages"] = []

    with pytest.raises(ValueError, match="stages_required"):
        LongHorizonTask.from_dict(data)


@pytest.mark.parametrize(
    "anchor",
    [
        {"type": "date", "month": 13, "day": 1, "timezone": "Asia/Shanghai"},
        {"type": "date", "month": 4, "day": 31, "timezone": "Asia/Shanghai"},
        {"type": "date", "month": 4, "day": 1, "timezone": "Mars/Olympus"},
    ],
)
def test_anchor_from_dict_rejects_invalid_date_or_timezone(anchor):
    with pytest.raises(ValueError):
        Anchor.from_dict(anchor)


@pytest.mark.parametrize(
    "due_at",
    ["not-a-datetime", "2026-11-20T09:00:00"],
)
def test_stage_from_dict_rejects_invalid_or_naive_due_at(due_at):
    data = make_task().stages[0].to_dict()
    data["due_at"] = due_at

    with pytest.raises(ValueError, match="invalid_due_at"):
        LongHorizonStage.from_dict(data)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda data: data.update(id="wrong_prefix"),
        lambda data: data.update(title=""),
        lambda data: data.update(status="unknown"),
        lambda data: data["stages"][0].update(id="wrong_prefix"),
        lambda data: data["stages"][0].update(title=""),
        lambda data: data["stages"][0].update(status="unknown"),
        lambda data: data["stages"][0].pop("plan"),
    ],
)
def test_models_reject_invalid_required_fields_ids_and_statuses(mutate):
    data = make_task().to_dict()
    mutate(data)

    with pytest.raises((TypeError, ValueError)):
        LongHorizonTask.from_dict(data)


def test_non_dict_stage_makes_whole_store_record_corrupt(tmp_path):
    corrupt = make_task().to_dict()
    corrupt["stages"].append("not-a-stage")
    valid = make_task()
    (tmp_path / "long_horizon_tasks.json").write_text(
        json.dumps({"tasks": [corrupt, valid.to_dict()]}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert load_long_horizon_tasks(tmp_path) == [valid]


def test_store_uses_workspace_root_and_upsert_is_atomic(tmp_path):
    task = make_task()

    upsert_long_horizon_task(tmp_path, task)

    store_path = tmp_path / "long_horizon_tasks.json"
    assert store_path.exists()
    assert get_long_horizon_task(tmp_path, task.id) == task
    assert list(tmp_path.glob("*.tmp")) == []


def test_atomic_upsert_preserves_old_file_and_cleans_temp_on_replace_failure(
    tmp_path, monkeypatch
):
    original = make_task()
    upsert_long_horizon_task(tmp_path, original)
    store_path = tmp_path / "long_horizon_tasks.json"
    old_content = store_path.read_bytes()

    def fail_replace(source, destination):
        raise OSError("replace failed")

    monkeypatch.setattr(long_horizon_store.os, "replace", fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        upsert_long_horizon_task(tmp_path, make_task())

    assert store_path.read_bytes() == old_content
    assert list(tmp_path.glob("*.tmp")) == []


def test_store_skips_corrupt_record_but_loads_valid_record(tmp_path):
    valid = make_task()
    (tmp_path / "long_horizon_tasks.json").write_text(
        json.dumps(
            {
                "tasks": [
                    {"id": "broken", "anchor": {"month": "not-a-month"}},
                    valid.to_dict(),
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    loaded = load_long_horizon_tasks(tmp_path)

    assert loaded == [valid]


def test_events_are_appended_in_workspace(tmp_path):
    append_event(
        tmp_path,
        task_id="lhc_one",
        stage_id="lhs_one",
        action="draft",
        extra={"source": "test"},
    )
    append_event(tmp_path, task_id="lhc_one", action="confirm")

    rows = [
        json.loads(line)
        for line in (tmp_path / "long_horizon_events.jsonl").read_text(
            encoding="utf-8"
        ).splitlines()
    ]
    assert [row["action"] for row in rows] == ["draft", "confirm"]
    assert rows[0]["task_id"] == "lhc_one"
    assert rows[0]["source"] == "test"


def test_event_extra_cannot_override_identity_or_action_fields(tmp_path):
    append_event(
        tmp_path,
        task_id="lhc_real",
        stage_id="lhs_real",
        action="confirm",
        extra={
            "at": "forged",
            "task_id": "lhc_forged",
            "stage_id": "lhs_forged",
            "action": "delete",
        },
    )

    row = json.loads(
        (tmp_path / "long_horizon_events.jsonl").read_text(encoding="utf-8")
    )
    assert row["at"] != "forged"
    assert row["task_id"] == "lhc_real"
    assert row["stage_id"] == "lhs_real"
    assert row["action"] == "confirm"


def test_kick_query_contains_brief_plan_progress_and_history():
    task = make_task()
    task.brief = "目标是按期发布，约束是不对外发送未审批内容。"
    current = task.stages[1]

    query = build_kick_query(task, current, history_snippet="用户：材料已整理完毕。")

    assert task.brief in query
    assert current.plan in query
    assert "阶段进度" in query
    assert "准备材料" in query
    assert "发布检查" in query
    assert "用户：材料已整理完毕。" in query
