# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for team skill link creation and event-loop-safe offloading."""

# pylint: disable=protected-access

import asyncio
import contextlib
import os
import stat
import sys
from pathlib import Path

import pytest

from jiuwenswarm.agents.harness.team import team_skill_links
from jiuwenswarm.agents.harness.team.team_skill_links import (
    _create_windows_junction,
    ensure_skill_dir_links,
    link_skill_dir,
    offload_link_sync,
)


def _make_skill(root: Path, name: str) -> Path:
    skill_dir = root / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(f"---\nname: {name}\n---\n", encoding="utf-8")
    return skill_dir


def _assert_link_resolves(link: Path, target: Path) -> None:
    """Assert the link exists and resolves to the expected target.

    On Windows ``link_skill_dir`` produces a junction (reparse point); on
    other platforms it falls through to a plain ``os.symlink`` symlink, so
    the shape assertion is platform-specific while the resolution assertion
    is shared.
    """
    assert os.path.lexists(link)
    if sys.platform == "win32":
        attributes = os.lstat(link).st_file_attributes
        assert attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT
    else:
        assert os.path.islink(link)
    assert link.resolve() == target.resolve()


_winapi_create_junction = getattr(getattr(team_skill_links, "_winapi", None), "CreateJunction", None)

requires_native_create_junction = pytest.mark.skipif(
    sys.platform != "win32" or _winapi_create_junction is None,
    reason="requires _winapi.CreateJunction on Windows",
)
requires_windows = pytest.mark.skipif(sys.platform != "win32", reason="Windows junctions")


@requires_native_create_junction
def test_create_windows_junction_native_avoids_subprocess(tmp_path, monkeypatch):
    """The native path creates a reparse-point junction without spawning cmd.exe."""
    spawned = []

    original_run = team_skill_links.subprocess.run

    def _spy_run(*args, **kwargs):
        spawned.append(args)
        return original_run(*args, **kwargs)

    monkeypatch.setattr(team_skill_links.subprocess, "run", _spy_run)
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"

    _create_windows_junction(target, link)

    _assert_link_resolves(link, target)
    assert not spawned, "native junction creation must not spawn cmd.exe"


@requires_windows
def test_cmd_fallback_creates_junction_when_winapi_unavailable(tmp_path, monkeypatch):
    """Without the private API the cmd.exe fallback still creates a working junction."""
    monkeypatch.setattr(team_skill_links, "_winapi", None)
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"

    _create_windows_junction(target, link)

    _assert_link_resolves(link, target)


def test_cmd_fallback_passes_decoding_options(monkeypatch, tmp_path):
    """The fallback decodes localized cmd.exe output instead of crashing (9-18 incident)."""
    captured = {}

    def _fake_run(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["kwargs"] = kwargs
        return type(
            "_Completed",
            (),
            {"returncode": 0, "stdout": "创建的联接", "stderr": ""},
        )()

    monkeypatch.setattr(team_skill_links, "_winapi", None)
    monkeypatch.setattr(team_skill_links.subprocess, "run", _fake_run)
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"

    _create_windows_junction(target, link)

    assert captured["kwargs"]["errors"] == "replace"
    assert captured["kwargs"]["capture_output"] is True
    if sys.platform == "win32":
        assert captured["kwargs"]["encoding"] == "oem"
    else:
        assert captured["kwargs"]["encoding"] == "utf-8"
    assert captured["cmd"][1:3] == ["/c", "mklink"]


def test_cmd_failure_raises_oserror_not_decode_error(monkeypatch, tmp_path):
    """A failing localized mklink raises OSError with readable text, never UnicodeDecodeError."""
    localized_error = "系统找不到指定的文件。"

    def _fake_run(cmd, **kwargs):
        return type(
            "_Completed",
            (),
            {"returncode": 1, "stdout": "", "stderr": localized_error},
        )()

    monkeypatch.setattr(team_skill_links, "_winapi", None)
    monkeypatch.setattr(team_skill_links.subprocess, "run", _fake_run)
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"

    with pytest.raises(OSError) as excinfo:
        _create_windows_junction(target, link)

    assert not isinstance(excinfo.value, UnicodeDecodeError)
    assert localized_error in str(excinfo.value)


@requires_windows
def test_native_rejection_falls_back_to_cmd_exe(tmp_path, monkeypatch):
    """When the native call rejects the creation, the cmd.exe fallback still creates the junction."""

    def _reject(_src, _dst):
        raise OSError("native rejected")

    monkeypatch.setattr(team_skill_links._winapi, "CreateJunction", _reject)
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"

    _create_windows_junction(target, link)

    _assert_link_resolves(link, target)


@requires_windows
def test_native_rejection_missing_target_falls_back_to_dangling_junction(tmp_path, monkeypatch):
    """Native rejection with a missing target: cmd.exe exits 0 and silently
    creates a dangling junction, which later prune runs remove."""
    def _reject(_src, _dst):
        raise OSError(2, "missing target")

    monkeypatch.setattr(team_skill_links._winapi, "CreateJunction", _reject)
    source = tmp_path / "skills_src"
    source.mkdir()
    target_root = tmp_path / "skills_tgt"
    target_root.mkdir()
    link = target_root / "dangling"

    _create_windows_junction(source / "missing_target", link)

    assert os.path.lexists(link)
    assert not link.exists(), "mklink /J must not validate the target"
    team_skill_links.prune_skill_dir_links(source, target_root)
    assert not os.path.lexists(link), "prune must remove the dangling junction"


def test_remove_skill_dir_link_tolerates_vanished_entry(tmp_path, monkeypatch):
    """A link removed by a concurrent sync between check and unlink is success."""
    link = tmp_path / "ghost"
    # Pretend the type check saw a link, but it is gone by the time the
    # removal syscall runs (concurrent prune raced us).
    monkeypatch.setattr(Path, "is_symlink", lambda self: True)

    team_skill_links.remove_skill_dir_link(link)  # must not raise FileNotFoundError

    assert not os.path.lexists(link)


@requires_windows
def test_remove_skill_dir_link_reraises_when_entry_remains(tmp_path, monkeypatch):
    """A removal error with the entry still present is not swallowed."""
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"
    _create_windows_junction(target, link)

    def _denied(_path):
        raise PermissionError(13, "Access denied")

    monkeypatch.setattr(team_skill_links.os, "rmdir", _denied)

    with pytest.raises(PermissionError):
        team_skill_links.remove_skill_dir_link(link)

    assert os.path.lexists(link)


def test_link_skill_dir_is_idempotent(tmp_path):
    """Linking twice keeps a single working link and never raises."""
    target = _make_skill(tmp_path, "skill-a")
    link = tmp_path / "link"

    link_skill_dir(target, link)
    link_skill_dir(target, link)

    _assert_link_resolves(link, target)


def test_ensure_skill_dir_links_skips_existing_entries(tmp_path):
    """Pre-existing target entries are left untouched (regression)."""
    source = tmp_path / "global_skills"
    _make_skill(source, "skill-a")
    _make_skill(source, "skill-b")
    target = tmp_path / "team_skills"
    target.mkdir()
    ordinary = target / "skill-a"
    ordinary.mkdir()
    (ordinary / "SKILL.md").write_text("---\nname: ordinary\n---\n", encoding="utf-8")

    ensure_skill_dir_links(source, target)

    assert (ordinary / "SKILL.md").read_text(encoding="utf-8") == "---\nname: ordinary\n---\n"
    _assert_link_resolves(target / "skill-b", source / "skill-b")


def test_offload_link_sync_runs_inline_without_running_loop(tmp_path):
    """Without a running loop the sync completes synchronously and returns None."""
    source = tmp_path / "global_skills"
    _make_skill(source, "skill-a")
    target = tmp_path / "team_skills"

    result = offload_link_sync(source, target)

    assert result is None
    _assert_link_resolves(target / "skill-a", source / "skill-a")


@pytest.mark.asyncio
async def test_offload_link_sync_schedules_on_running_loop_and_keeps_loop_responsive(tmp_path):
    """On the event loop the sync runs in a worker thread while the loop keeps ticking."""
    source = tmp_path / "global_skills"
    _make_skill(source, "skill-a")
    target = tmp_path / "team_skills"

    ticks = 0

    async def _heartbeat():
        nonlocal ticks
        while True:
            ticks += 1
            await asyncio.sleep(0)

    heartbeat = asyncio.create_task(_heartbeat())
    try:
        pending = offload_link_sync(source, target)
        assert pending is not None, "a running loop must offload instead of running inline"
        await pending
    finally:
        heartbeat.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat

    assert ticks >= 1, "event loop must stay responsive while the sync runs off thread"
    _assert_link_resolves(target / "skill-a", source / "skill-a")
