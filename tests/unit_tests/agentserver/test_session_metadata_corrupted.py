# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""会话列表对损坏 metadata.json 的呈现契约。"""

from __future__ import annotations

from jiuwenswarm.server.runtime.session import session_metadata


def test_corrupted_metadata_file_is_listed_with_marker(tmp_path, monkeypatch) -> None:
    """文件存在但读取失败必须打 corrupted 标记，不得伪装成合法旧会话。"""
    root = tmp_path / "sessions"
    (root / "broken").mkdir(parents=True)
    (root / "broken" / "metadata.json").write_text("{ truncated", encoding="utf-8")
    (root / "legacy").mkdir(parents=True)
    monkeypatch.setattr(session_metadata, "get_agent_sessions_dir", lambda: root)
    session_metadata._METADATA_CACHE.clear()

    listed, total = session_metadata.get_all_sessions_metadata(limit=20, offset=0)

    assert total == 2
    by_id = {item["session_id"]: item for item in listed}
    assert by_id["broken"].get("metadata_corrupted") is True
    assert "metadata_corrupted" not in by_id["legacy"]
    session_metadata._METADATA_CACHE.clear()
