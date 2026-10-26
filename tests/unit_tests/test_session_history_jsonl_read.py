# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.server.runtime.agent_adapter.interface import _history_user_content
from jiuwenswarm.server.runtime.session.session_history import (
    collapse_file_content_blocks,
    _read_history_jsonl,
)


def test_collapse_file_content_blocks_restores_at_path():
    raw = (
        "how many files\n"
        "[upload]\n\n"
        '<file-content path="C:\\Users\\a\\file.txt">\n'
        "line1\nline2\n"
        "</file-content>\n"
    )
    collapsed = collapse_file_content_blocks(raw)
    assert "file-content" not in collapsed
    assert collapsed.startswith("how many files")
    assert "@C:\\Users\\a\\file.txt" in collapsed


def test_collapse_file_content_quotes_paths_with_spaces():
    raw = '<file-content path="D:\\docs\\my file.txt">hello</file-content>'
    assert collapse_file_content_blocks(raw) == '@"D:\\docs\\my file.txt"'


def test_history_user_content_strips_inlined_file_body():
    query = (
        "summarize\n[upload]\n"
        '<file-content path="D:\\a.txt">huge body</file-content>'
    )
    got = _history_user_content({"mode": "agent"}, query)
    assert "file-content" not in got
    assert got.startswith("summarize")
    assert "@D:\\a.txt" in got


def test_read_history_jsonl_keeps_user_row_with_unicode_line_separator(tmp_path: Path):
    """Inlined bodies may contain U+2028; splitlines() must not break the JSONL row."""
    body = "hello\u2028world"
    content = (
        f"ask me\n[upload]\n"
        f'<file-content path="C:\\t.txt">{body}</file-content>'
    )
    record = {
        "id": "r1:user",
        "role": "user",
        "content": content,
        "timestamp": 1.0,
    }
    path = tmp_path / "history.jsonl"
    path.write_text(json.dumps(record, ensure_ascii=False) + "\n", encoding="utf-8")

    rows = _read_history_jsonl(path)
    assert len(rows) == 1
    assert rows[0]["role"] == "user"
    assert "ask me" in rows[0]["content"]
    assert "<file-content" not in rows[0]["content"]
    assert "@C:\\t.txt" in rows[0]["content"]
