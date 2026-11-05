"""set_user_home() must move every path derived from the home, not just the home."""
from __future__ import annotations

from pathlib import Path

import pytest

import jiuwenswarm.common.utils as utils


@pytest.fixture
def isolated_paths(monkeypatch: pytest.MonkeyPatch) -> None:
    # Every cache the setter touches goes through monkeypatch, so the real values come
    # back after the test however it ends.
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.delenv("JIUWENSWARM_HOME", raising=False)
    for name in (
        "_user_home", "_workspace_base_dir", "_initialized",
        "_config_dir", "_workspace_dir", "_root_dir",
    ):
        monkeypatch.setattr(utils, name, getattr(utils, name))


def test_a_second_home_moves_a_workspace_root_already_cached(
    tmp_path: Path, isolated_paths: None
) -> None:
    """The workspace root is cached on its first lookup, and the getter returns the
    cache before it consults the home; a setter that left it would keep every path on
    the first home -- in practice the developer's real one, cached at import time.
    """
    first, second = tmp_path / "first", tmp_path / "second"

    utils.set_user_home(first)
    assert utils.get_user_workspace_dir() == first / ".jiuwenswarm"  # fills the cache

    utils.set_user_home(second)
    assert utils.get_user_workspace_dir() == second / ".jiuwenswarm", (
        "换了 home 之后工作区根必须跟着换"
    )


def test_initialized_keeps_the_caches_as_documented(
    tmp_path: Path, isolated_paths: None
) -> None:
    first, second = tmp_path / "first", tmp_path / "second"

    utils.set_user_home(first)
    cached = utils.get_user_workspace_dir()
    utils.set_user_home(second, initialized=True)
    assert utils.get_user_workspace_dir() == cached
