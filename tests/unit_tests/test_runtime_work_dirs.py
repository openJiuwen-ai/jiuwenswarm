# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

import importlib.util
import logging
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from jiuwenswarm.runtime_work_dirs import RUNTIME_DIR_VARS, ensure_runtime_work_dirs

_KEYS = [key for key, _ in RUNTIME_DIR_VARS]


def test_posix_leaves_environment_untouched(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    assert ensure_runtime_work_dirs(env, is_windows=False, candidate_roots=[tmp_path]) == {}
    assert env == {}
    assert not any(tmp_path.iterdir())


def test_windows_unset_vars_point_to_first_writable_root(tmp_path: Path) -> None:
    env: dict[str, str] = {}
    root = tmp_path / "rt"
    applied = ensure_runtime_work_dirs(env, is_windows=True, candidate_roots=[root])
    assert applied == {"DEPLOY_DIR": str(root / "deploys"), "DIST_DIR": str(root / "dist")}
    assert env == applied
    for value in applied.values():
        assert Path(value).is_absolute()
        assert Path(value).is_dir()


def test_explicit_values_are_kept(tmp_path: Path) -> None:
    env = {"DEPLOY_DIR": "E:\\custom\\deploys", "DIST_DIR": "  "}
    applied = ensure_runtime_work_dirs(env, is_windows=True, candidate_roots=[tmp_path])
    assert applied == {"DIST_DIR": str(tmp_path / "dist")}
    assert env["DEPLOY_DIR"] == "E:\\custom\\deploys"
    assert not (tmp_path / "deploys").exists()


@pytest.mark.parametrize("value", ["/tmp/deploys", "\\tmp\\deploys", "C:tmp\\deploys"])
def test_drive_root_bound_values_are_replaced(tmp_path: Path, value: str, caplog) -> None:
    env = {"DEPLOY_DIR": value, "DIST_DIR": "E:\\custom\\dist"}
    with caplog.at_level(logging.WARNING, logger="jiuwenswarm.early"):
        applied = ensure_runtime_work_dirs(env, is_windows=True, candidate_roots=[tmp_path])
    assert applied == {"DEPLOY_DIR": str(tmp_path / "deploys")}
    assert env["DIST_DIR"] == "E:\\custom\\dist"
    assert value in caplog.text


def test_plain_relative_value_is_kept(tmp_path: Path) -> None:
    env = {"DEPLOY_DIR": "deploys", "DIST_DIR": "E:\\custom\\dist"}
    assert ensure_runtime_work_dirs(env, is_windows=True, candidate_roots=[tmp_path]) == {}
    assert env["DEPLOY_DIR"] == "deploys"


def test_dotenv_override_is_corrected_after_load(tmp_path: Path, monkeypatch) -> None:
    from jiuwenswarm import dotenv_early, runtime_work_dirs

    dotenv_file = tmp_path / ".env"
    dotenv_file.write_text("DEPLOY_DIR=/tmp/deploys\n", encoding="utf-8")
    root = tmp_path / "rt"
    original = runtime_work_dirs.ensure_runtime_work_dirs
    monkeypatch.setattr(
        runtime_work_dirs,
        "ensure_runtime_work_dirs",
        lambda: original(is_windows=True, candidate_roots=[root]),
    )
    monkeypatch.setattr(dotenv_early, "is_enterprise", lambda: False)
    monkeypatch.setenv("DIST_DIR", "E:\\custom\\dist")
    monkeypatch.delenv("DEPLOY_DIR", raising=False)
    dotenv_early.load_dotenv_runtime(dotenv_file, override=True)
    assert os.environ["DEPLOY_DIR"] == str(root / "deploys")


def test_falls_back_when_first_root_is_not_creatable(tmp_path: Path) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("a file where a directory is needed", encoding="utf-8")
    good = tmp_path / "good"
    env: dict[str, str] = {}
    applied = ensure_runtime_work_dirs(env, is_windows=True, candidate_roots=[blocked, good])
    assert applied["DEPLOY_DIR"] == str(good / "deploys")
    assert applied["DIST_DIR"] == str(good / "dist")


def test_all_roots_fail_logs_guidance_and_keeps_env(tmp_path: Path, caplog) -> None:
    blocked = tmp_path / "blocked"
    blocked.write_text("x", encoding="utf-8")
    env: dict[str, str] = {}
    with caplog.at_level(logging.ERROR, logger="jiuwenswarm.early"):
        assert ensure_runtime_work_dirs(env, is_windows=True, candidate_roots=[blocked]) == {}
    assert env == {}
    message = caplog.text
    assert "DEPLOY_DIR / DIST_DIR" in message
    assert str(blocked) in message


def test_default_roots_prefer_temp_then_data_dir(tmp_path: Path) -> None:
    env = {"JIUWENSWARM_DATA_DIR": str(tmp_path / "data")}
    applied = ensure_runtime_work_dirs(env, is_windows=True)
    expected_root = Path(tempfile.gettempdir()) / "openjiuwen-runtime"
    assert applied["DEPLOY_DIR"] == str(expected_root / "deploys")


def test_undeterminable_home_does_not_raise(monkeypatch) -> None:

    def _no_home(cls) -> Path:
        raise RuntimeError("Could not determine home directory.")

    monkeypatch.setattr(Path, "home", classmethod(_no_home))
    env: dict[str, str] = {}
    applied = ensure_runtime_work_dirs(env, is_windows=True)
    expected_root = Path(tempfile.gettempdir()) / "openjiuwen-runtime"
    assert applied["DEPLOY_DIR"] == str(expected_root / "deploys")


@pytest.mark.skipif(os.name != "nt", reason="drive-root join only happens on Windows")
@pytest.mark.skipif(
    importlib.util.find_spec("openjiuwen_runtime") is None, reason="openjiuwen_runtime not installed"
)
def test_runtime_settings_no_longer_resolve_to_drive_root() -> None:
    """End to end: importing jiuwenswarm first keeps runtime Settings off ``<drive>:\\tmp``."""
    env = {k: v for k, v in os.environ.items() if k not in _KEYS}
    code = (
        "import jiuwenswarm\n"
        "from openjiuwen_runtime.foundation.config import settings\n"
        "print(settings.DEPLOY_DIR); print(settings.DIST_DIR)\n"
    )
    out = subprocess.run(
        [sys.executable, "-c", code], env=env, capture_output=True, text=True, timeout=120, check=True
    ).stdout.split()
    expected_root = (Path(tempfile.gettempdir()) / "openjiuwen-runtime").resolve()
    for value in out[-2:]:
        path = Path(value)
        assert path.parent == expected_root
        assert path.parent != Path(path.anchor) / "tmp"
