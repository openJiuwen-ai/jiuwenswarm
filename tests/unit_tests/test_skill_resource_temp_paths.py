"""Shipped skill resources must not name fixed paths in the shared temp directory.

The system temporary directory is shared between every account on a host, so a
fixed name there is owned by whichever account creates it first and refused to
all the others. These guards cover the three shipped skill resources that used
to build such a name.
"""

from __future__ import annotations

import getpass
import importlib.util
import os
import stat
import sys
import tempfile
from pathlib import Path
from types import ModuleType

import pytest

from jiuwenswarm.common.utils import get_builtin_skills_dir

_SKILL_CREATOR = "skill-creator-normal"


def _load_module_by_path(name: str, path: Path) -> ModuleType:
    """Import a shipped resource that is not reachable as a package module."""
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None, path
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture()
def plots(tmp_path, monkeypatch) -> ModuleType:
    """The trace analyzer's plotting module, with the temp dir redirected."""
    path = (
        get_builtin_skills_dir()
        / "ascend-moe-optimizer-trace-analyzer"
        / "analyzers"
        / "plots.py"
    )
    assert path.is_file(), path
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))
    return _load_module_by_path("_trace_analyzer_plots", path)


def test_matplotlib_config_dir_differs_between_users(plots, tmp_path, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
    first = Path(plots._mpl_config_dir())
    monkeypatch.setattr(os, "getuid", lambda: 4243, raising=False)
    second = Path(plots._mpl_config_dir())

    assert first != second
    assert first.parent == tmp_path and second.parent == tmp_path
    assert first.name == "trace_analysis_matplotlib-4242"
    assert second.name == "trace_analysis_matplotlib-4243"


def test_matplotlib_config_dir_is_reused_by_the_same_user(plots, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
    assert plots._mpl_config_dir() == plots._mpl_config_dir()


@pytest.mark.skipif(sys.platform == "win32", reason="POSIX mode bits")
def test_matplotlib_config_dir_is_private(plots, monkeypatch):
    monkeypatch.setattr(os, "getuid", lambda: 4242, raising=False)
    created = Path(plots._mpl_config_dir())

    assert created.is_dir()
    assert stat.S_IMODE(created.stat().st_mode) == 0o700


def test_user_scope_falls_back_to_a_sanitised_user_name(plots, monkeypatch):
    """Platforms without POSIX user ids still get a name they can put in a path."""
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setattr(getpass, "getuser", lambda: "dom\\a in/../user")

    scope = plots._user_scope()

    assert "/" not in scope
    assert "\\" not in scope
    assert os.sep not in scope
    assert scope == "dom_a_in_.._user"


def test_user_scope_survives_an_unresolvable_user_name(plots, monkeypatch):
    def _raise() -> str:
        raise KeyError("no passwd entry")

    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setattr(getpass, "getuser", _raise)

    assert plots._user_scope() == "unknown"


def test_skill_creator_report_path_is_not_built_from_the_temp_root():
    """The live report is allocated by mkstemp, not named from a timestamp."""
    source = (
        get_builtin_skills_dir() / _SKILL_CREATOR / "scripts" / "run_loop.py"
    ).read_text(encoding="utf-8")

    assert "tempfile.mkstemp(" in source
    assert "tempfile.gettempdir()" not in source


def test_skill_creator_guidance_recommends_a_scratch_directory():
    """The authoring skill teaches the pattern that its own output will copy."""
    text = (get_builtin_skills_dir() / _SKILL_CREATOR / "SKILL.md").read_text(
        encoding="utf-8"
    )

    assert "mktemp -d" in text
    assert "Write to a temp file (e.g., `/tmp/eval_review_" not in text
    assert "Copy to `/tmp/skill-name/`" not in text
    assert "stage in `/tmp/` first" not in text
