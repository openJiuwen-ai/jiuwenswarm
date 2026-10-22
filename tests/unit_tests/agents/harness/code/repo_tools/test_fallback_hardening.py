# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""The built-in search that answers when ripgrep is missing or broken.

Desktop and source installs often have no ripgrep, and a bundled one can be
killed on launch (a quarantined binary on macOS exits with SIGKILL). These
tests drive the real ``CodeSearchTool`` and engine on temporary trees:

* the fallback scan runs off the event loop and stops at its time budget;
* a file or time limit is reported to the model instead of a silent
  "no matches";
* a ripgrep that crashes, cannot start or fails without output hands the call
  to the built-in search, and a crashed binary is not run again right away;
* pattern errors are still errors;
* ``.gitignore`` files apply with git semantics (nested files, negation,
  anchoring, directory-only patterns), matching ripgrep when one is present.
"""

from __future__ import annotations

import asyncio
import os
import stat
import time
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.code.repo_tools import CodeSearchTool, _common
from jiuwenswarm.agents.harness.code.repo_tools._common import (
    GitIgnore,
    python_search_bounded,
    reset_rg_health,
)
from jiuwenswarm.agents.harness.code.repo_tools.repo_map import _walk


@pytest.fixture(autouse=True)
def _fresh_rg_health():
    reset_rg_health()
    yield
    reset_rg_health()


def _run(tool: CodeSearchTool, **inputs):
    return asyncio.run(tool.invoke(inputs))


def _script(path: Path, body: str) -> str:
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return str(path)


def _small_repo(tmp_path: Path) -> Path:
    root = tmp_path / "repo"
    (root / "src").mkdir(parents=True)
    (root / "src" / "app.py").write_text(
        "def needle_fn():\n    return 1\n", encoding="utf-8"
    )
    return root


# ─── off the event loop, bounded ────────────────────────────


def _big_tree(root: Path, files: int, lines: int = 120) -> None:
    body = "".join(
        f"value_{i} = {i}  # filler line for the scan\n" for i in range(lines)
    )
    for index in range(files):
        sub = root / f"pkg{index // 200}"
        sub.mkdir(parents=True, exist_ok=True)
        (sub / f"mod{index}.py").write_text(body, encoding="utf-8")
    (root / "pkg0" / "target.py").write_text(
        "def needle_fn():\n    pass\n", encoding="utf-8"
    )


def test_fallback_scan_does_not_block_the_event_loop(tmp_path: Path, no_rg) -> None:
    root = tmp_path / "big"
    _big_tree(root, files=3000)
    tool = CodeSearchTool(lambda: str(root), agent_id="t")

    async def scenario() -> tuple[float, float, int]:
        gaps: list[float] = []
        done = asyncio.Event()

        async def ticker() -> None:
            last = time.perf_counter()
            while not done.is_set():
                await asyncio.sleep(0.005)
                now = time.perf_counter()
                gaps.append(now - last)
                last = now

        tick = asyncio.create_task(ticker())
        start = time.perf_counter()
        out = await tool.invoke({"pattern": r"value_\d+ = 7\b"})
        elapsed = time.perf_counter() - start
        done.set()
        await tick
        assert out.success and out.data["engine"] == "python"
        return elapsed, max(gaps or [elapsed]), len(gaps)

    elapsed, worst_gap, ticks = asyncio.run(scenario())
    assert elapsed > 0.1, "the tree must be large enough to measure"
    # The loop kept running while the scan worked in a thread.
    assert ticks >= 5
    assert worst_gap < max(0.1, elapsed / 3)


def test_time_budget_stops_the_scan_and_the_model_is_told(
    tmp_path: Path, no_rg
) -> None:
    root = tmp_path / "big"
    _big_tree(root, files=400)
    tool = CodeSearchTool(lambda: str(root), agent_id="t", timeout=0.000001)
    out = _run(tool, pattern="needle_fn")
    assert out.success
    assert out.data["timed_out"] is True
    content = out.data["content"]
    assert "search timed out, results are partial" in content
    assert "stopped at its time limit" in content and "install ripgrep" in content


def test_file_cap_is_reported_not_silent(tmp_path: Path, no_rg, monkeypatch) -> None:
    root = tmp_path / "big"
    _big_tree(root, files=60)
    monkeypatch.setattr(_common, "PYTHON_SEARCH_MAX_FILES", 10)
    tool = CodeSearchTool(lambda: str(root), agent_id="t")
    out = _run(tool, pattern="needle_fn")
    assert out.success and out.data["timed_out"] is False
    warning = out.data["warning"]
    assert "stopped at its file limit after 10 files" in warning
    assert "results may be incomplete" in out.data["content"]


def test_complete_scan_has_no_limit_warning(tmp_path: Path) -> None:
    root = _small_repo(tmp_path)
    scan = python_search_bounded(
        root,
        root,
        pattern="needle_fn",
        literal=True,
        case="smart",
        context=0,
        globs=(),
        extensions=None,
        max_count_per_file=5,
        deadline=time.monotonic() + 30,
    )
    assert scan.stopped == "" and scan.warning == "" and len(scan.hits) == 1


# ─── a broken ripgrep hands over to the built-in search ─────


def test_ripgrep_killed_by_signal_falls_back_and_is_set_aside(tmp_path: Path) -> None:
    root = _small_repo(tmp_path)
    marker = tmp_path / "rg-runs"
    fake = _script(tmp_path / "rg", f'echo run >> "{marker}"\nkill -9 $$')
    tool = CodeSearchTool(lambda: str(root), agent_id="t", rg_path=fake)

    first = _run(tool, pattern="needle_fn")
    assert first.success and first.data["engine"] == "python"
    assert [f["path"] for f in first.data["files"]] == ["src/app.py"]
    assert "ripgrep crashed; used the built-in search" in first.data["content"]

    second = _run(tool, pattern="needle_fn")
    assert second.success and second.data["engine"] == "python"
    # The dead binary is not started again on every call.
    assert marker.read_text(encoding="utf-8").count("run") == 1


@pytest.mark.parametrize("code", [126, 127, 3])
def test_ripgrep_exit_codes_it_never_uses_fall_back(tmp_path: Path, code: int) -> None:
    root = _small_repo(tmp_path)
    fake = _script(tmp_path / "rg", f"echo 'cannot execute' >&2\nexit {code}")
    out = _run(
        CodeSearchTool(lambda: str(root), agent_id="t", rg_path=fake),
        pattern="needle_fn",
    )
    assert out.success and out.data["engine"] == "python"
    assert out.data["total_matches"] == 1
    assert "ripgrep crashed" in out.data["warning"]


def test_ripgrep_that_cannot_start_falls_back(tmp_path: Path) -> None:
    root = _small_repo(tmp_path)
    bogus = tmp_path / "rg"
    bogus.write_bytes(b"\x00\x01\x02 not a program")
    bogus.chmod(bogus.stat().st_mode | stat.S_IXUSR)
    out = _run(
        CodeSearchTool(lambda: str(root), agent_id="t", rg_path=str(bogus)),
        pattern="needle_fn",
    )
    assert out.success and out.data["engine"] == "python"
    assert out.data["total_matches"] == 1
    assert "ripgrep could not start" in out.data["warning"]


def test_ripgrep_error_without_output_falls_back_but_is_retried(tmp_path: Path) -> None:
    root = _small_repo(tmp_path)
    marker = tmp_path / "rg-runs"
    fake = _script(
        tmp_path / "rg", f'echo run >> "{marker}"\necho "some io error" >&2\nexit 2'
    )
    tool = CodeSearchTool(lambda: str(root), agent_id="t", rg_path=fake)
    first = _run(tool, pattern="needle_fn")
    assert (
        first.success
        and first.data["engine"] == "python"
        and first.data["total_matches"] == 1
    )
    assert (
        "ripgrep failed (some io error); used the built-in search"
        in first.data["warning"]
    )
    _run(tool, pattern="needle_fn")
    # An ordinary ripgrep error is about this search, not a broken binary.
    assert marker.read_text(encoding="utf-8").count("run") == 2


def test_ripgrep_pattern_errors_are_still_errors(tmp_path: Path) -> None:
    root = _small_repo(tmp_path)
    fake = _script(
        tmp_path / "rg", "echo 'regex parse error: unclosed group' >&2\nexit 2"
    )
    out = _run(
        CodeSearchTool(lambda: str(root), agent_id="t", rg_path=fake), pattern="("
    )
    assert not out.success and "invalid pattern" in out.error
    assert _common.rg_usable(fake)  # a bad pattern does not set the binary aside


def test_fallback_pattern_errors_are_still_errors(tmp_path: Path, no_rg) -> None:
    root = _small_repo(tmp_path)
    out = _run(CodeSearchTool(lambda: str(root), agent_id="t"), pattern="(")
    assert not out.success and "invalid pattern" in out.error


# ─── .gitignore with git semantics ──────────────────────────


def _ignore_repo(tmp_path: Path) -> Path:
    root = tmp_path / "ig"
    files = {
        "a.log": "",
        "keep.log": "",
        "build/y.py": "",
        "sub/build/x.py": "",
        "docs/a.md": "",
        "docs/deep/b.md": "",
        "sub/generated/z.py": "",
        "sub/x.tmp": "",
        "sub/important.tmp": "",
        "sub/kept.py": "",
        "src/cache/c.py": "",
        "src/cachefile.py": "",
        "logs/today.txt": "",
    }
    for rel in files:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("needle_fn = 1\n", encoding="utf-8")
    (root / ".gitignore").write_text(
        "# comment\n*.log\n!keep.log\n/build/\ndocs/*.md\n**/cache/\nlogs\n",
        encoding="utf-8",
    )
    (root / "sub" / ".gitignore").write_text(
        "generated/\n*.tmp\n!important.tmp\n", encoding="utf-8"
    )
    return root


_EXPECTED_VISIBLE = {
    "keep.log",
    "sub/build/x.py",
    "docs/deep/b.md",
    "sub/important.tmp",
    "sub/kept.py",
    "src/cachefile.py",
}


def test_gitignore_matcher_follows_git_semantics(tmp_path: Path) -> None:
    root = _ignore_repo(tmp_path)
    gi = GitIgnore(root)
    assert gi.is_ignored("a.log", False)
    assert not gi.is_ignored("keep.log", False)  # negation
    assert gi.is_ignored("build", True) and gi.is_ignored("build/y.py", False)
    assert not gi.is_ignored(
        "sub/build/x.py", False
    )  # "/build/" is anchored at the root
    assert gi.is_ignored("docs/a.md", False)
    assert not gi.is_ignored("docs/deep/b.md", False)  # "*" does not cross "/"
    assert gi.is_ignored("sub/generated/z.py", False)  # nested .gitignore
    assert gi.is_ignored("sub/x.tmp", False)
    assert not gi.is_ignored("sub/important.tmp", False)  # nested negation
    assert gi.is_ignored("src/cache/c.py", False)  # "**/cache/"
    assert not gi.is_ignored("src/cachefile.py", False)
    assert gi.is_ignored("logs/today.txt", False)  # bare name matches a directory too
    assert not gi.is_ignored("sub", True)
    # A directory-only pattern does not match a file of that name.
    (root / "notes").write_text("x", encoding="utf-8")
    (root / ".gitignore").write_text("notes/\n", encoding="utf-8")
    assert not GitIgnore(root).is_ignored("notes", False)


def test_git_info_exclude_counts_as_root_rules(tmp_path: Path) -> None:
    root = _small_repo(tmp_path)
    (root / ".git" / "info").mkdir(parents=True)
    (root / ".git" / "info" / "exclude").write_text("src/\n", encoding="utf-8")
    assert GitIgnore(root).is_ignored("src/app.py", False)


def test_fallback_search_honours_nested_gitignore(tmp_path: Path, no_rg) -> None:
    root = _ignore_repo(tmp_path)
    out = _run(
        CodeSearchTool(lambda: str(root), agent_id="t"),
        pattern="needle_fn",
        max_results=50,
    )
    assert out.success and out.data["engine"] == "python"
    assert {f["path"] for f in out.data["files"]} == _EXPECTED_VISIBLE


def test_ripgrep_agrees_with_the_fallback_on_gitignore(tmp_path: Path, rg_bin) -> None:
    if not rg_bin:
        pytest.skip("ripgrep not available")
    root = _ignore_repo(tmp_path)
    out = _run(
        CodeSearchTool(lambda: str(root), agent_id="t", rg_path=rg_bin),
        pattern="needle_fn",
        max_results=50,
    )
    assert out.success and out.data["engine"] == "ripgrep"
    assert {f["path"] for f in out.data["files"]} == _EXPECTED_VISIBLE


def test_repo_map_walk_honours_nested_gitignore(tmp_path: Path) -> None:
    root = _ignore_repo(tmp_path)
    _, _, per_dir = _walk(root)
    listed = {
        (f"{d}/{name}" if d != "." else name)
        for d, (names, _) in per_dir.items()
        for name in names
    }
    assert "sub/generated/z.py" not in listed and "sub/x.tmp" not in listed
    assert "sub/kept.py" in listed and "sub/important.tmp" in listed
    assert "build/y.py" not in listed and "sub/build/x.py" in listed


def test_explicit_rg_override_is_used_when_executable(tmp_path: Path) -> None:
    fake = _script(tmp_path / "rg", "exit 1")
    assert _common.find_rg_binary(fake) == fake
    assert os.access(fake, os.X_OK)
