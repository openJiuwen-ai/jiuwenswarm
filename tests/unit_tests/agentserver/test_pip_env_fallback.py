"""POSIX venv discovery falls back without changing Windows host trust checks."""

import os
import sys
from types import SimpleNamespace

import pytest

from jiuwenswarm.server.runtime import pip_env


@pytest.fixture
def posix_runtime(tmp_path, monkeypatch):
    monkeypatch.setattr(pip_env, "os", SimpleNamespace(name="posix", environ={}))
    managed = tmp_path / "managed"
    executable = managed / "bin" / "python"
    executable.parent.mkdir(parents=True)
    executable.touch()
    monkeypatch.setattr(pip_env, "get_runtime_venv_dir", lambda: managed)
    host = tmp_path / "host" / "python"
    host.parent.mkdir()
    host.touch()
    monkeypatch.setattr(sys, "executable", str(host))
    return managed, executable, host


@pytest.mark.parametrize("config", ["home = missing", "executable = {self}", "home = \x00"])
def test_broken_existing_posix_venv_falls_back_to_host(posix_runtime, config):
    managed, executable, host = posix_runtime
    (managed / "pyvenv.cfg").write_text(config.format(self=executable), encoding="utf-8")
    assert pip_env.resolve_base_python() == host.resolve()


def test_posix_venv_home_can_contain_only_python3(posix_runtime):
    managed, executable, host = posix_runtime
    python3 = host.with_name("python3")
    host.rename(python3)
    (managed / "pyvenv.cfg").write_text(f"home = {python3.parent}{os.linesep}", encoding="utf-8")
    assert pip_env.resolve_python_base(executable) == python3.resolve()
    assert pip_env.resolve_base_python() == python3.resolve()
