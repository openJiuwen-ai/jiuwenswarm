# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for caching the git facts that cannot change between turns."""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.agent_adapter import interface_deep
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import _resolve_stable_git_facts


@pytest.fixture(autouse=True)
def _clear_cache():
    interface_deep._STABLE_GIT_FACTS.clear()
    yield
    interface_deep._STABLE_GIT_FACTS.clear()


def _make_runner(calls: list[list[str]], *, is_repo: bool = True) -> callable:
    """Return a git runner double recording every command it is asked to run."""

    def _run(args: list[str]) -> str:
        calls.append(args)
        if args[:2] == ["rev-parse", "--is-inside-work-tree"]:
            return "true" if is_repo else ""
        if args[:2] == ["config", "user.name"]:
            return "alan"
        if args[0] == "rev-parse" and "--verify" in args:
            return "abc123" if args[-1] == "origin/master" else ""
        if args[:2] == ["rev-parse", "--absolute-git-dir"]:
            return "/repo/.git"
        return ""

    return _run


def test_stable_facts_are_resolved_once_per_project() -> None:
    """Repeat turns must not re-spawn the git subprocesses behind these answers."""
    calls: list[list[str]] = []
    runner = _make_runner(calls)

    first = _resolve_stable_git_facts("/usr/bin/git", "/repo", runner)
    for _ in range(4):
        _resolve_stable_git_facts("/usr/bin/git", "/repo", runner)

    assert first.is_repo is True
    assert first.user_name == "alan"
    # origin/main is probed first and misses, origin/master answers.
    assert first.main_branch == "origin/master"
    # Resolved via git, so it is right for a subdirectory, worktree or submodule.
    assert first.head_file.endswith("HEAD")
    # is-inside-work-tree + user.name + two probes + git-dir, then nothing.
    assert len(calls) == 5


def test_non_repo_skips_the_remaining_probes() -> None:
    """Outside a work tree there is nothing further worth asking git."""
    calls: list[list[str]] = []

    facts = _resolve_stable_git_facts("/usr/bin/git", "/plain/dir", _make_runner(calls, is_repo=False))

    assert facts.is_repo is False
    assert facts.user_name == ""
    assert facts.main_branch == ""
    assert calls == [["rev-parse", "--is-inside-work-tree"]]


def test_distinct_projects_are_cached_separately() -> None:
    """Two project dirs must not serve each other's facts."""
    calls: list[list[str]] = []
    runner = _make_runner(calls)

    _resolve_stable_git_facts("/usr/bin/git", "/repo/a", runner)
    calls.clear()
    _resolve_stable_git_facts("/usr/bin/git", "/repo/b", runner)

    assert calls, "a second project must resolve its own facts"


def test_git_binary_change_is_not_served_from_cache() -> None:
    """A different toolchain may answer differently, so it re-resolves."""
    calls: list[list[str]] = []
    runner = _make_runner(calls)

    _resolve_stable_git_facts("/usr/bin/git", "/repo", runner)
    calls.clear()
    _resolve_stable_git_facts("/opt/homebrew/bin/git", "/repo", runner)

    assert calls, "a different git binary must resolve its own facts"


def test_cached_facts_are_immutable() -> None:
    """Shared cache entries must not be editable by one caller."""
    facts = _resolve_stable_git_facts("/usr/bin/git", "/repo", _make_runner([]))

    with pytest.raises(Exception):
        facts.user_name = "someone-else"
