# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest
from prompt_toolkit.utils import get_cwidth

from jiuwenswarm.channels.process_cli import main as cli_main
from jiuwenswarm.channels.process_cli import session_entry
from jiuwenswarm.runtime import session_catalog
from jiuwenswarm.runtime.session_catalog import SessionListResult, SessionSummary


def _result(*items: SessionSummary, total: int | None = None) -> SessionListResult:
    return SessionListResult(
        sessions=items,
        total=total if total is not None else len(items),
        limit=20,
        offset=0,
    )


def _session(session_id: str, **kwargs) -> SessionSummary:
    return SessionSummary(
        session_id=session_id,
        channel_id="process_cli",
        title="title",
        mode="agent.code.normal",
        work_mode="code",
        **kwargs,
    )


def _execute(arguments: list[str]) -> int:
    parser = cli_main.build_parser()
    return session_entry.execute_session_query(
        parser.parse_args(arguments), parser, arguments
    )


def test_query_uses_owned_catalog_without_runtime_start(monkeypatch, capsys):
    calls = []

    def list_sessions(request, *, include_team, read_only):
        calls.append((request, include_team, read_only))
        return _result(_session("process_cli_full_id"))

    monkeypatch.setattr(session_catalog, "list_sessions", list_sessions)
    assert _execute(["--latest-session", "--search", "my project"]) == 0
    assert capsys.readouterr().out == "process_cli_full_id\n"
    request, include_team, read_only = calls[0]
    assert request.channel_id == "process_cli"
    assert request.search == "my project" and request.limit == 1 and request.offset == 0
    assert include_team and read_only


@pytest.mark.parametrize(
    "arguments,search,limit,offset,latest",
    [
        (["--list"], "", 20, 0, False),
        (["--latest"], "", 1, 0, True),
        (["--list-sessions", "--sea", "my project"], "my project", 20, 0, False),
        (
            ["--list", "--sea", "my project", "--lim", "7", "--off", "2"],
            "my project",
            7,
            2,
            False,
        ),
        (
            ["--list", "--sea=my project", "--lim=7", "--off=2"],
            "my project",
            7,
            2,
            False,
        ),
        (["--latest", "--sea=my project"], "my project", 1, 0, True),
    ],
)
def test_query_accepts_unique_abbreviations(
    arguments, search, limit, offset, latest, monkeypatch, capsys
):
    calls = []

    def list_sessions(request, *, include_team, read_only):
        calls.append((request, include_team, read_only))
        return _result(_session("process_cli_full_id"))

    monkeypatch.setattr(session_catalog, "list_sessions", list_sessions)
    assert _execute(arguments) == 0
    request, include_team, read_only = calls[0]
    assert request.channel_id == "process_cli"
    assert (request.search, request.limit, request.offset) == (search, limit, offset)
    assert include_team and read_only
    output = capsys.readouterr().out
    assert "process_cli_full_id" in output
    if latest:
        assert output == "process_cli_full_id\n"


def test_list_keeps_full_ids_and_sanitizes_multiline_metadata(monkeypatch, capsys):
    item = SessionSummary(
        session_id="process_cli_1234567890abcdef_9876543210abcdef",
        channel_id="process_cli",
        title="two\nlines\x1b",
        mode="team.work.normal",
        work_mode="work",
        project_dir="C:/a\tb",
        last_message_at=float("inf"),
    )
    monkeypatch.setattr(
        session_catalog, "list_sessions", lambda *a, **kw: _result(item)
    )
    assert _execute(["--list-sessions"]) == 0
    output = capsys.readouterr().out
    assert item.session_id in output and "team.work.normal" in output
    assert "two lines " in output and "C:/a b" in output
    assert "\x1b" not in output and len(output.splitlines()) == 3


@pytest.mark.parametrize(
    "title,project_dir",
    [("中文任务", "D:/项目"), ("Cafe\u0301", "D:/work"), ("", "")],
)
def test_list_aligns_headers_and_values_by_terminal_cells(
    title, project_dir, monkeypatch, capsys
):
    headers = ("会话ID", "最近消息时间", "模式", "标题", "项目目录")
    values = [
        (
            "process_cli_1234567890abcdef_9876543210abcdef",
            "-",
            "team.work.normal",
            title,
            project_dir,
        ),
        ("process_cli_short", "-", "agent.code.normal", "OK", "C:/x"),
    ]
    items = [
        SessionSummary(
            session_id=row[0],
            channel_id="process_cli",
            mode=row[2],
            work_mode="code",
            title=row[3],
            project_dir=row[4],
            last_message_at=0,
        )
        for row in values
    ]
    monkeypatch.setattr(
        session_catalog, "list_sessions", lambda *a, **kw: _result(*items)
    )
    assert _execute(["--list-sessions"]) == 0
    output = capsys.readouterr().out
    lines = output.splitlines()[:-1]
    assert len(lines) == 3 and lines[0].startswith("会话ID  ")
    assert "\t" not in output
    rows = [headers, *values]
    for column in range(len(headers)):
        starts = {
            get_cwidth(line[: line.index(row[column])])
            for line, row in zip(lines, rows)
            if row[column]
        }
        assert len(starts) == 1
    for line, row in zip(lines, rows):
        for left, right in zip(row, row[1:]):
            if left and right:
                gap = line[line.index(left) + len(left) : line.index(right)]
                assert len(gap) >= 2 and gap.isspace()


def test_list_past_last_page_still_renders_headers(monkeypatch, capsys):
    result = SessionListResult(sessions=(), total=1, limit=20, offset=2)
    monkeypatch.setattr(session_catalog, "list_sessions", lambda *a, **kw: result)
    assert _execute(["--list-sessions", "--offset", "2"]) == 0
    output = capsys.readouterr().out
    assert output.startswith("会话ID  ")
    assert "本页 0 条；匹配共 1 条；offset=2；limit=20" in output
    assert len(output.splitlines()) == 2


@pytest.mark.parametrize(
    "arguments",
    [
        ["--list-sessions", "--limit", "0"],
        ["--list-sessions", "--limit", "201"],
        ["--list-sessions", "--offset", "-1"],
        ["--list-sessions", "--search", "x" * 201],
        ["--list-sessions", "task"],
        ["--list-sessions", "--mode", "code.normal"],
        ["--list-sessions", "--run-json", "missing.json"],
        ["--list-sessions", "--cwd", "/nonexistent"],
        ["--list-sessions", "--output", "human"],
        ["--latest-session", "--limit", "1"],
        ["--latest-session", "--offset", "0"],
        ["--search", "test"],
        ["--limit", "10", "task"],
        ["--offset", "0"],
        ["--list", "--ses", "process_cli_full_id"],
        ["--list", "--time=30"],
        ["--list", "--mo", "code.normal"],
        ["--list", "--out", "human"],
        ["--list", "--lim", "0"],
        ["--list", "--off", "-1"],
        ["--latest", "--lim=1"],
        ["--latest", "--off=0"],
        ["--sea", "test"],
        ["--list", "--l"],
        ["--list", "--o", "0"],
    ],
)
def test_invalid_queries_fail_before_storage(arguments, monkeypatch):
    def forbidden(*args, **kwargs):
        pytest.fail("invalid command accessed storage")

    monkeypatch.setattr(session_catalog, "list_sessions", forbidden)
    with pytest.raises(SystemExit) as caught:
        _execute(arguments)
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "option,expected", [("--list-sessions", 0), ("--latest-session", 1)]
)
def test_empty_query_has_explicit_status(option, expected, monkeypatch, capsys):
    monkeypatch.setattr(session_catalog, "list_sessions", lambda *a, **kw: _result())
    assert _execute([option]) == expected
    captured = capsys.readouterr()
    if expected:
        assert captured.out == "" and "未找到匹配" in captured.err
    else:
        assert "没有匹配" in captured.out


def test_query_failure_does_not_leak_storage_details(monkeypatch, capsys):
    def fail(*args, **kwargs):
        raise OSError("private-secret")

    monkeypatch.setattr(session_catalog, "list_sessions", fail)
    assert _execute(["--list-sessions"]) == 1
    captured = capsys.readouterr()
    assert "会话查询失败" in captured.err
    assert "private-secret" not in captured.out + captured.err


def test_query_main_exits_without_entering_repl(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["cli", "--list-sessions"])
    monkeypatch.setattr(session_catalog, "list_sessions", lambda *a, **kw: _result())
    with pytest.raises(SystemExit) as caught:
        cli_main.main()
    assert caught.value.code == 0 and "没有匹配" in capsys.readouterr().out


@pytest.mark.parametrize("flag", ["-h", "--help", "-help"])
def test_help_exits_without_loading_runtime_or_storage(flag, tmp_path):
    script = """
import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname.startswith(("jiuwenswarm.runtime", "jiuwenswarm.server")):
            raise AssertionError("help imported Runtime/storage")
sys.meta_path.insert(0, Guard())
from jiuwenswarm.channels.process_cli.main import main
sys.argv = ["cli", sys.argv[1]]
main()
"""
    env = dict(os.environ, JIUWENSWARM_DATA_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    result = subprocess.run(
        [sys.executable, "-c", script, flag],
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
        check=False,
    )
    assert result.returncode == 0, result.stderr
    assert "--list-sessions" in result.stdout and "--latest-session" in result.stdout
    assert "完整会话ID" in result.stdout
    assert not list(tmp_path.iterdir())


def test_real_subprocess_query_is_fresh_read_only_and_channel_scoped(tmp_path):
    sessions = tmp_path / "agent" / "sessions"
    for sid, channel, mode, stamp in [
        ("process_cli_old", "process_cli", "agent.code.normal", 10),
        ("process_cli_team", "process_cli", "team.work.normal", 20),
        ("web_foreign", "web", "agent.code.normal", 100),
    ]:
        folder = sessions / sid
        folder.mkdir(parents=True)
        (folder / "metadata.json").write_text(
            json.dumps(
                {
                    "session_id": sid,
                    "channel_id": channel,
                    "mode": mode,
                    "title": sid,
                    "last_message_at": stamp,
                }
            ),
            encoding="utf-8",
        )
        (folder / "history.jsonl").write_text("fixture\n", encoding="utf-8")
    before = {p: p.read_bytes() for p in sessions.rglob("*") if p.is_file()}
    env = dict(os.environ, JIUWENSWARM_DATA_DIR=str(tmp_path), PYTHONIOENCODING="utf-8")
    guard = """
import sys
class Guard:
    def find_spec(self, fullname, path=None, target=None):
        if fullname in ("jiuwenswarm.runtime.service", "jiuwenswarm.server.runtime.agent_adapter.interface_deep", "jiuwenswarm.server.runtime.agent_adapter.interface_code"):
            raise AssertionError("query started Agent Runtime")
sys.meta_path.insert(0, Guard())
from jiuwenswarm.channels.process_cli.main import main
sys.argv = ["cli", *sys.argv[1:]]
main()
"""

    def run(*args):
        result = subprocess.run(
            [sys.executable, "-c", guard, *args],
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=30,
            check=False,
        )
        assert result.returncode == 0, result.stderr
        return result.stdout

    assert run("--latest-session") == "process_cli_team\n"
    assert run("--latest") == "process_cli_team\n"
    output = run("--list-sessions", "--limit", "1", "--offset", "1")
    assert "process_cli_old" in output and "process_cli_team" not in output
    assert run("--list", "--lim=1", "--off=1") == output
    assert "web_foreign" not in run("--list-sessions")
    assert run("--list") == run("--list-sessions")
    file = sessions / "process_cli_old" / "metadata.json"
    metadata = json.loads(file.read_text(encoding="utf-8"))
    metadata["last_message_at"] = 30
    file.write_text(json.dumps(metadata), encoding="utf-8")
    before[file] = file.read_bytes()
    assert run("--latest-session") == "process_cli_old\n"
    assert run("--latest-session", "--search=team") == "process_cli_team\n"
    assert run("--latest", "--sea=team") == "process_cli_team\n"
    assert "process_cli_team" in run("--list", "--sea", "team")
    assert {p: p.read_bytes() for p in sessions.rglob("*") if p.is_file()} == before
