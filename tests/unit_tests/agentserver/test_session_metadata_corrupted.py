# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""会话列表对损坏 metadata.json 的呈现契约。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.session import session_metadata
from jiuwenswarm.server.runtime.session.session_info import to_session_info

# (说明, metadata.json 内容, 是否应打损坏标记)
# 合法空对象 "{}" 不算损坏：解析成功且为 dict，只是"空元数据"。
_CORRUPT_CASES = [
    ("truncated-json", "{ truncated", True),
    ("empty-file", "", True),
    ("non-dict-json", "[]", True),
    ("legal-empty-object", "{}", False),
]


@pytest.mark.parametrize(("case", "content", "expected_corrupted"), _CORRUPT_CASES)
def test_corrupted_metadata_file_is_listed_with_marker(tmp_path, monkeypatch, case, content, expected_corrupted) -> None:
    """文件存在但读取失败必须打 corrupted 标记，不得伪装成合法旧会话；合法空对象不算损坏。"""
    root = tmp_path / "sessions"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "metadata.json").write_text(content, encoding="utf-8")
    (root / "legacy").mkdir(parents=True)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: root)
    session_metadata._METADATA_CACHE.clear()

    listed, total = session_metadata.get_all_sessions_metadata(limit=20, offset=0)

    assert total == 2
    by_id = {item["session_id"]: item for item in listed}
    assert by_id["broken"].get("metadata_corrupted", False) is expected_corrupted
    assert "metadata_corrupted" not in by_id["legacy"]
    # 投影层（Web/网关 session.list 的唯一出口）必须透传标记，
    # 否则消费方拿不到字段，标记只在后端日志生效。
    assert to_session_info(by_id["broken"])["metadata_corrupted"] is expected_corrupted
    assert to_session_info(by_id["legacy"])["metadata_corrupted"] is False
    session_metadata._METADATA_CACHE.clear()


def test_missing_metadata_file_is_not_marked(tmp_path, monkeypatch) -> None:
    """文件缺失（读取成功返回 {}）不算读取失败，不得打损坏标记。"""
    root = tmp_path / "sessions"
    (root / "legacy").mkdir(parents=True)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: root)
    session_metadata._METADATA_CACHE.clear()

    listed, total = session_metadata.get_all_sessions_metadata(limit=20, offset=0)

    assert total == 1
    assert "metadata_corrupted" not in listed[0]
    session_metadata._METADATA_CACHE.clear()
