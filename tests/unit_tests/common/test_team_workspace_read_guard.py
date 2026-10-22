# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team document reads preserve overrides and degrade without stalling tools."""

import threading
import time
import weakref
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import team_workspace_read_guard as guard
from jiuwenswarm.common.background_work import BoundedBackgroundWork
from openjiuwen.agent_teams.team_workspace.workspace_cache import WorkspaceCache


@pytest.fixture
def guarded_cache(monkeypatch, tmp_path):
    monkeypatch.setattr(WorkspaceCache, "_load_tools", WorkspaceCache._load_tools)
    monkeypatch.setattr(WorkspaceCache, "invalidate", WorkspaceCache.invalidate)
    monkeypatch.setattr(guard, "_states", weakref.WeakKeyDictionary())
    monkeypatch.setattr(guard, "_reads", BoundedBackgroundWork(max_workers=2, name="test-team-read"))
    monkeypatch.setattr(guard, "_FIRST_READ_WAIT", 0.01)
    store = SimpleNamespace(team_workspace_root=lambda name: tmp_path / name)
    return lambda name="team": WorkspaceCache(store, name)


def _until(predicate):
    deadline = time.monotonic() + 2
    while not predicate():
        assert time.monotonic() < deadline
        time.sleep(0.002)


def test_real_documents_and_parameters_preserve_customized_values(
    monkeypatch, tmp_path, guarded_cache,
):
    root = tmp_path / "team/prompts/tool"
    (root / "team").mkdir(parents=True)
    (root / "team/build_team.cn.md").write_text(
        "---\nupdated_at: 1\n---\nCUSTOM TEAM DESCRIPTION", encoding="utf-8",
    )
    (root / "tool.param.cn.md").write_text('{"build_team.title": "CUSTOM TITLE"}', encoding="utf-8")
    monkeypatch.setattr(guard, "_FIRST_READ_WAIT", 1.0)
    guard.install_team_workspace_read_guard()
    installed = WorkspaceCache._load_tools
    guard.install_team_workspace_read_guard()
    assert WorkspaceCache._load_tools is installed
    cache = guarded_cache()
    assert cache.get_tool_md("build_team") == "CUSTOM TEAM DESCRIPTION"
    assert cache.get_tool_param("build_team", "title") == "CUSTOM TITLE"


def test_slow_snapshot_is_single_flight_and_never_publishes_partial_values(
    monkeypatch, guarded_cache,
):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def slow(snapshot):
        calls.append(threading.get_ident())
        snapshot._tool_md_values["build_team"] = "PARTIAL"
        entered.set()
        assert release.wait(3)
        snapshot._tool_md_values["build_team"] = "COMPLETE"

    monkeypatch.setattr(WorkspaceCache, "_load_tools", slow)
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    try:
        assert cache.get_tool_md("build_team") is None
        assert entered.wait(1)
        began = time.monotonic()
        for _ in range(100):
            assert cache.get_tool_md("build_team") is None
        assert time.monotonic() - began < 0.2
        assert len(calls) == 1 and calls[0] != threading.get_ident()
        future = guard._states[cache].future
    finally:
        release.set()
    future.result(1)
    assert cache.get_tool_md("build_team") == "COMPLETE"
    assert cache not in guard._states


def test_invalidated_read_cannot_publish_into_the_next_run(monkeypatch, guarded_cache):
    entered, release = threading.Event(), threading.Event()
    calls = []

    def read(snapshot):
        calls.append(snapshot)
        if len(calls) == 1:
            entered.set()
            assert release.wait(3)
            snapshot._tool_md_values["build_team"] = "OBSOLETE"
        else:
            snapshot._tool_md_values["build_team"] = "CURRENT"

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    try:
        assert cache.get_tool_md("build_team") is None
        assert entered.wait(1)
        obsolete = guard._states[cache].future
        cache.invalidate()
        _until(lambda: cache.get_tool_md("build_team") == "CURRENT")
    finally:
        release.set()
    obsolete.result(1)
    assert cache.get_tool_md("build_team") == "CURRENT"


def test_assembler_values_win_over_a_slow_disk_snapshot(monkeypatch, guarded_cache):
    release = threading.Event()

    def read(snapshot):
        assert release.wait(3)
        snapshot._tool_md_values["build_team"] = "OLD DISK VALUE"
        snapshot._tool_params[("build_team", "title")] = "OLD TITLE"

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    try:
        cache.get_tool_md("build_team")
        future = guard._states[cache].future
        cache.fill_tool_md("build_team", "NEW ASSEMBLER VALUE")
        cache.fill_tool_param("build_team", "title", "NEW TITLE")
    finally:
        release.set()
    future.result(1)
    assert cache.get_tool_md("build_team") == "NEW ASSEMBLER VALUE"
    assert cache.get_tool_param("build_team", "title") == "NEW TITLE"


def test_busy_or_failed_read_uses_defaults_and_can_recover(monkeypatch, guarded_cache):
    calls = []

    def read(snapshot):
        calls.append(snapshot)
        if len(calls) == 1:
            raise OSError("temporarily unreadable document")
        snapshot._tool_md_values["build_team"] = "RECOVERED"

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    monkeypatch.setattr(guard, "_ERROR_RETRY_DELAY", 0)
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    assert cache.get_tool_md("build_team") is None
    _until(lambda: cache.get_tool_md("build_team") == "RECOVERED")


def test_many_caches_cannot_create_unbounded_threads(monkeypatch, guarded_cache):
    release = threading.Event()
    calls = []

    def read(snapshot):
        calls.append(snapshot)
        assert release.wait(3)

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    guard.install_team_workspace_read_guard()
    caches = [guarded_cache(f"team-{i}") for i in range(100)]
    try:
        for cache in caches:
            assert cache.get_tool_md("build_team") is None
        assert len(calls) == 2
        futures = [state.future for state in guard._states.values() if state.future is not None]
    finally:
        release.set()
    for future in futures:
        future.result(1)


def test_bundled_fallback_uses_warmed_files_without_loop_filesystem_reads(monkeypatch):
    from pathlib import Path
    from openjiuwen.agent_teams.tools.locales import make_translator

    guard.preload_bundled_team_descriptions()

    def unexpected_read(*args, **kwargs):
        pytest.fail("bundled tool descriptions were read on the event loop")

    monkeypatch.setattr(Path, "read_text", unexpected_read)
    for language in ("cn", "en"):
        assert make_translator(language)("build_team")


@pytest.mark.parametrize("attribute", ["_store", "_team_name", "_lang"])
def test_incompatible_cache_uses_bundled_descriptions_once_per_run(
    monkeypatch, guarded_cache, caplog, attribute,
):
    from openjiuwen.agent_teams.tools.locales import make_translator

    monkeypatch.setattr(guard.logger, "handlers", [caplog.handler])
    monkeypatch.setattr(guard.logger, "propagate", False)
    reads = []

    def read(snapshot):
        reads.append(snapshot)
        snapshot._tool_md_values["build_team"] = "RECOVERED"

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    monkeypatch.setattr(guard, "_FIRST_READ_WAIT", 1)
    guard.preload_bundled_team_descriptions()
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    original_value = getattr(cache, attribute)
    monkeypatch.delattr(cache, attribute)
    translate = make_translator("cn", ws_cache=cache)
    expected = make_translator("cn")("build_team")
    for _ in range(3):
        assert translate("build_team") == expected
    warnings = [record for record in caplog.records if "incompatible" in record.message]
    assert len(warnings) == 1
    assert cache not in guard._states or guard._states[cache].future is None
    assert not reads
    cache.fill_tool_md("build_team", "ASSEMBLER VALUE")
    assert translate("build_team") == "ASSEMBLER VALUE"
    monkeypatch.setattr(cache, attribute, original_value, raising=False)
    cache.invalidate()
    assert cache.get_tool_md("build_team") == "RECOVERED"
    assert len(reads) == 1


def test_incompatible_snapshot_cannot_publish_partial_descriptions(monkeypatch, guarded_cache):
    def read(snapshot):
        snapshot._tool_md_values["build_team"] = "INCOMPATIBLE SNAPSHOT"
        snapshot._tool_params = None

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    monkeypatch.setattr(guard, "_FIRST_READ_WAIT", 1)
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    assert cache.get_tool_md("build_team") is None
    assert cache.get_tool_param("build_team", "title") is None


def test_thread_start_failure_degrades_then_retries(monkeypatch, guarded_cache):
    def read(snapshot):
        snapshot._tool_md_values["build_team"] = "RECOVERED"

    def fail(*args, **kwargs):
        raise RuntimeError("cannot start another thread")

    monkeypatch.setattr(WorkspaceCache, "_load_tools", read)
    monkeypatch.setattr(guard, "_BUSY_RETRY_DELAY", 0)
    guard.install_team_workspace_read_guard()
    cache = guarded_cache()
    with monkeypatch.context() as patch:
        patch.setattr(threading.Thread, "start", fail)
        assert cache.get_tool_md("build_team") is None
    _until(lambda: cache.get_tool_md("build_team") == "RECOVERED")
