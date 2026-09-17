# Copyright (c) Huawei Technologies, Co., Ltd. 2026. All rights reserved.
"""回归：rewind / compact 必须显式传 project_dir，且在写 metadata 之前解析.

``metadata.json`` 是非原子的原地覆写（``_write_metadata_sync`` 用
``fpath.write_text``），且由后台线程执行。若 ``truncate_file_ops_by_timestamp``
自己去调 ``_get_project_dir_from_metadata``，会撞上刚被 ``update_session_metadata``
截断到一半的文件 → ``JSONDecodeError`` → 静默返回 ``None`` → 扫不到项目目录下的
file_ops → 整个清理变成无声空操作（文件从此失去回滚能力，且没有任何报错）。

这是实测撞到的偶发失败：同样的 /rewind 2，一次标记成功、一次完全没动。
"""

from unittest.mock import MagicMock, patch

import pytest


def _history():
    return [
        {"role": "user", "content": "turn one", "timestamp": 1000.0},
        {"role": "assistant", "content": "ok", "timestamp": 1010.0},
        {"role": "user", "content": "turn two", "timestamp": 1100.0},
        {"role": "assistant", "content": "ok", "timestamp": 1110.0},
    ]


@pytest.fixture
def wired(tmp_path):
    """接管 history/metadata 读写，返回 (diff_service_mock, call_order)。"""
    call_order: list[str] = []

    ds = MagicMock()
    ds.resolve_project_dir.side_effect = lambda _sid: (
            call_order.append("resolve_project_dir") or str(tmp_path)
    )
    ds.truncate_file_ops_by_timestamp.side_effect = lambda *a, **k: call_order.append(
        "truncate_file_ops"
    )

    def _update_meta(**_kw):
        call_order.append("update_session_metadata")

    hist_path = tmp_path / "history.jsonl"
    hist_path.write_text("", encoding="utf-8")

    with (
        patch("jiuwenswarm.server.utils.diff_service.get_diff_service", return_value=ds),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_read_history_path",
            return_value=hist_path,
        ),
        patch(
            "jiuwenswarm.agents.harness.common.session_ops_service.load_history_records",
            return_value=_history(),
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_history.truncate_history_records",
            return_value={"remaining_records": 2, "removed_records": 2},
        ),
        patch(
            "jiuwenswarm.server.runtime.session.session_metadata.update_session_metadata",
            side_effect=_update_meta,
        ),
    ):
        yield ds, call_order


def test_rewind_passes_explicit_project_dir(wired, tmp_path):
    from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session

    ds, _order = wired
    rewind_session(session_id="sess-race", turn_index=2)

    ds.truncate_file_ops_by_timestamp.assert_called_once()
    kwargs = ds.truncate_file_ops_by_timestamp.call_args.kwargs
    assert kwargs.get("project_dir") == str(tmp_path), (
        "必须显式传 project_dir，否则下游自行推断会撞上 metadata 写入竞态"
    )
    # conversation 回退不动工作区文件，必须软删除（#2241）
    assert kwargs.get("soft") is True


def test_project_dir_resolved_before_metadata_write(wired):
    """顺序断言：解析必须发生在 update_session_metadata 之前。"""
    from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session

    _ds, order = wired
    rewind_session(session_id="sess-race", turn_index=2)

    assert "resolve_project_dir" in order
    assert "update_session_metadata" in order
    assert order.index("resolve_project_dir") < order.index("update_session_metadata"), (
        f"解析晚于元数据写入，竞态窗口仍在：{order}"
    )


def test_compact_passes_explicit_project_dir(wired, tmp_path):
    from jiuwenswarm.agents.harness.common.session_ops_service import (
        compact_partial_session,
    )

    ds, order = wired
    with patch(
            "jiuwenswarm.agents.harness.common.session_ops_service._write_records_to_path"
    ):
        compact_partial_session(session_id="sess-race", turn_index=2, direction="from")

    ds.truncate_file_ops_by_timestamp.assert_called_once()
    kwargs = ds.truncate_file_ops_by_timestamp.call_args.kwargs
    assert kwargs.get("project_dir") == str(tmp_path)
    assert kwargs.get("soft") is True
    assert order.index("resolve_project_dir") < order.index("update_session_metadata")
