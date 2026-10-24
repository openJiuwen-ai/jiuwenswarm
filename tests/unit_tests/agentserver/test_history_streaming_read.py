"""流式读取会话历史：与全量载入的等价性 + 内存上限回归。

背景：``load_history_records()`` 会把整份 JSONL 读成一个 Python 字符串、再
``split("\\n")`` 出逐行副本、再物化全部 dict。线上一个 115 MB / 45.6k 条记录的
会话实测单次调用峰值 ~939 MB；只要某个调用点其实只扫描一遍或只取最后 N 条，
这个峰值就是纯浪费，也是 AgentServer 撞 cgroup 上限的放大器。

``iter_history_records()`` 逐行解析，峰值与历史长度无关。
"""
from __future__ import annotations

import json
from pathlib import Path

from jiuwenswarm.server.runtime.session import session_history as sh


def _line(record: dict, *, ensure_ascii: bool = False) -> str:
    return json.dumps(record, ensure_ascii=ensure_ascii)


def _write(path: Path, lines: list[str]) -> None:
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _pin_history_path(monkeypatch, path: Path) -> None:
    """让两个读取器都指向同一个临时文件，绕开会话目录解析。"""
    monkeypatch.setattr(
        sh, "get_read_history_path", lambda session_id, subagent_id=None: path
    )


def test_iter_history_records_matches_eager_read(tmp_path, monkeypatch) -> None:
    """等价性：同一份脏数据，流式与全量必须给出完全相同的记录序列。"""
    path = tmp_path / "history.jsonl"
    _write(
        path,
        [
            _line({"id": "1", "role": "user", "content": "hello"}),
            "",
            "   ",
            "{not json}",
            "[1, 2, 3]",
            # 内联正文里的 U+2028 不能把一条记录切成两半（历史回归 bug）。
            _line({"id": "2", "role": "user", "content": "before\u2028after"}),
            _line(
                {
                    "id": "3",
                    "role": "assistant",
                    "content": "final",
                    "event_type": "chat.final",
                }
            ),
            _line(
                {
                    "id": "4",
                    "role": "user",
                    "content": "<file-content>X</file-content>tail",
                }
            ),
        ],
    )
    _pin_history_path(monkeypatch, path)

    eager = sh.load_history_records("sess")
    streamed = list(sh.iter_history_records("sess"))

    assert streamed == eager
    assert [record["id"] for record in streamed] == ["1", "2", "3", "4"]
    # 清理规则同样生效：<file-content> 块在两条路径上都被折叠。
    assert streamed[3]["content"] == eager[3]["content"]


def test_iter_history_records_handles_crlf(tmp_path, monkeypatch) -> None:
    """CRLF 行尾下两个读取器的记录边界一致。"""
    path = tmp_path / "history.jsonl"
    path.write_text(
        "\r\n".join([_line({"id": "a"}), _line({"id": "b"})]) + "\r\n",
        encoding="utf-8",
    )
    _pin_history_path(monkeypatch, path)

    assert list(sh.iter_history_records("sess")) == sh.load_history_records("sess")
    assert [record["id"] for record in sh.iter_history_records("sess")] == ["a", "b"]


def test_iter_history_records_missing_file_is_empty(tmp_path, monkeypatch) -> None:
    _pin_history_path(monkeypatch, tmp_path / "history.jsonl")

    assert sh.load_history_records("sess") == []
    assert list(sh.iter_history_records("sess")) == []


def test_iter_history_records_legacy_json_matches_eager_read(
    tmp_path, monkeypatch
) -> None:
    """非 JSONL 的兼容格式走 ``_read_history``，两个入口结果一致。"""
    path = tmp_path / "history.json"
    path.write_text(
        json.dumps([{"id": "1"}, {"id": "2"}]), encoding="utf-8"
    )
    _pin_history_path(monkeypatch, path)

    assert list(sh.iter_history_records("sess")) == sh.load_history_records("sess")


def test_iter_history_records_peak_memory_is_bounded(tmp_path, monkeypatch):
    """内存回归：流式的峰值必须远小于全量载入。"""
    import tracemalloc

    path = tmp_path / "history.jsonl"
    payload = "x" * 2048
    _write(
        path,
        [
            _line({"id": str(i), "role": "assistant", "content": payload})
            for i in range(4000)
        ],
    )
    _pin_history_path(monkeypatch, path)

    tracemalloc.start()
    eager = sh.load_history_records("sess")
    _, eager_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert len(eager) == 4000

    tracemalloc.start()
    count = sum(1 for _ in sh.iter_history_records("sess"))
    _, stream_peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert count == 4000

    # 实测流式峰值在百 KB 量级，全量在数十 MB 量级；留 10x 余量避免抖动。
    assert stream_peak * 10 < eager_peak, (stream_peak, eager_peak)


def test_team_member_names_scans_history_streaming(tmp_path, monkeypatch) -> None:
    """调用点回归：diff 状态只需要 team.member.spawned 事件。"""
    from jiuwenswarm.server.runtime.session import git_diff_status as gds

    path = tmp_path / "history.jsonl"
    _write(
        path,
        [
            _line(
                {
                    "extra": {
                        "event": {
                            "type": "team.member.spawned",
                            "name": "alice",
                            "member_id": "m1",
                        }
                    }
                }
            ),
            _line({"extra": {"event": {"type": "team.message", "name": "bob"}}}),
            _line(
                {
                    "extra": {
                        "event": {"type": "team.member.spawned", "name": "carol"}
                    }
                }
            ),
        ],
    )
    _pin_history_path(monkeypatch, path)

    assert gds._session_team_member_names("sess") == ["alice", "m1", "carol"]
