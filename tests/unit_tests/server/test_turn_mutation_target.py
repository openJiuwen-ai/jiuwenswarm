"""Regression coverage for stale review cards after conversation rewind."""

from types import SimpleNamespace
from unittest.mock import patch

import pytest

from jiuwenswarm.server.utils.diff_service import DiffService


@pytest.mark.parametrize("replacement_turns", [0, 2])
def test_summaries_exclude_rewound_snapshots(replacement_turns):
    history = [{"role": "user", "request_id": "r1", "timestamp": 100.0}]
    history += [
        {"role": "user", "request_id": f"new-{index}", "timestamp": 300.0 + index}
        for index in range(replacement_turns)
    ]
    snapshots = {
        f"cs-{index}": {
            "turnIndex": index,
            "request_id": f"r{index}",
            "change_set_id": f"cs-{index}",
            "files": {f"file-{index}": {}},
            "status": "discarded",
        }
        for index in (1, 2)
    }
    service = DiffService()
    with (
        patch.object(service, "_compute_turn_diffs", return_value=[]),
        patch.object(service, "_enrich_with_change_sets"),
        patch.object(service, "_read_history", return_value=history),
        patch.object(service, "_load_change_sets", return_value=[
            {"turn_index": index, "change_set_id": f"cs-{index}"}
            for index in (1, 2)
        ]),
        patch.object(service, "_load_turn_snapshot", side_effect=lambda _, key: snapshots[key]),
    ):
        # The current discarded turn remains redoable; removed/replaced turns
        # must never reach the frontend's positional message binding fallback.
        assert service.get_turn_diff_summaries("session") == [snapshots["cs-1"]]


@pytest.mark.parametrize("action", ["discard", "redo"])
@pytest.mark.parametrize("expected_id", ["stale-cs", "", 123])
def test_stale_card_cannot_mutate_another_turn(action, expected_id):
    from jiuwenswarm.agents.harness.common import session_ops_service as ops
    from jiuwenswarm.server.runtime.gateway_adapter import project_adapter as adapter

    service = DiffService()
    with (
        patch.object(adapter, "_resolve_turn_operation", return_value=(
            SimpleNamespace(project_dir="/proj"), "session", None,
        )),
        patch.object(ops, "get_last_modified_turn_info", return_value={
            "turn_index": 1, "timestamp": 100.0,
        }),
        patch("jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots", return_value=[]),
        patch("jiuwenswarm.server.utils.diff_service.get_diff_service", return_value=service),
        patch.object(service, "get_turn_diff", return_value={
            "change_set_id": "current-cs",
            "status": "discarded" if action == "redo" else "completed",
        }),
        patch.object(ops, "restore_session_files") as restore,
        patch.object(ops, "redo_session_files") as redo,
        patch.object(service, "mark_turn_discarded") as mark,
        patch.object(service, "unmark_turn_discarded") as unmark,
        patch.object(service, "truncate_file_ops_by_timestamp") as truncate,
    ):
        payload, failure = getattr(adapter, f"_run_{action}_turn_changes")({
            "change_set_id": expected_id,
        })
        assert payload == {}
        assert failure["code"] == "TURN_TARGET_CHANGED"
        for mutation in (restore, redo, mark, unmark, truncate):
            mutation.assert_not_called()


@pytest.mark.parametrize("action", ["discard", "redo"])
def test_target_lookup_failure_returns_internal_error(action):
    """定位最后修改轮失败 → INTERNAL_ERROR,而非 NO_TURN_* 业务空态。

    回归: locator 吞错返回 {turn_index: 0} 时,discard/redo 会报
    "没有可撤销/可重新应用的修改",把内部错误伪装成空态;现在
    locator 向上传播,入口按 INTERNAL_ERROR 拒绝且不触碰工作区。
    """
    from jiuwenswarm.agents.harness.common import session_ops_service as ops
    from jiuwenswarm.server.runtime.gateway_adapter import project_adapter as adapter

    with (
        patch.object(adapter, "_resolve_turn_operation", return_value=(
            SimpleNamespace(project_dir="/proj"), "session", None,
        )),
        patch.object(ops, "get_last_modified_turn_info", side_effect=KeyError("timestamp")),
        patch("jiuwenswarm.server.runtime.session.git_diff_status.get_session_extra_history_roots", return_value=[]),
        patch.object(ops, "restore_session_files") as restore,
        patch.object(ops, "redo_session_files") as redo,
    ):
        payload, failure = getattr(adapter, f"_run_{action}_turn_changes")({})

    assert payload == {}
    assert failure["code"] == "INTERNAL_ERROR"
    assert "failed to locate last modified turn" in failure["error"]
    restore.assert_not_called()
    redo.assert_not_called()
