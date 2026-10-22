"""jiuwenswarm-init resolves the default workspace like the runtime does."""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm import init_workspace
from jiuwenswarm.common import utils


@pytest.fixture
def isolated_workspace_cache(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setattr(utils, "_workspace_base_dir", None)
    monkeypatch.setattr(utils, "_user_home", None)
    yield
    monkeypatch.setattr(utils, "_workspace_base_dir", None)
    monkeypatch.setattr(utils, "_user_home", None)


def _capture_init(monkeypatch: pytest.MonkeyPatch) -> dict[str, object]:
    captured: dict[str, object] = {}

    def fake_init_user_workspace(*, overwrite: bool, workspace_dir: Path) -> Path:
        captured["overwrite"] = overwrite
        captured["workspace_dir"] = workspace_dir
        return workspace_dir

    monkeypatch.setattr(init_workspace, "init_user_workspace", fake_init_user_workspace)
    return captured


def test_run_init_honours_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_workspace_cache: None
) -> None:
    data_dir = tmp_path / "data"
    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(data_dir))
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path / "home"))
    captured = _capture_init(monkeypatch)

    assert init_workspace.run_init() == 0
    assert captured["workspace_dir"] == data_dir
    assert captured["overwrite"] is False


def test_run_init_defaults_to_home_dot_jiuwenswarm(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, isolated_workspace_cache: None
) -> None:
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path / "home"))
    captured = _capture_init(monkeypatch)

    assert init_workspace.run_init() == 0
    assert captured["workspace_dir"] == tmp_path / "home" / ".jiuwenswarm"
