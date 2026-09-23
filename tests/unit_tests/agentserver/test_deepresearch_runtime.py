from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common.local_env_config import (
    bind_task_env_overlay,
    reset_task_env_overlay,
)


def _fake_venv_python(tmp_path: Path, *, executable: bool = True) -> Path:
    venv_root = tmp_path / "deepresearch-venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    python = bin_dir / "python"
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755 if executable else 0o644)
    return python


def test_resolve_python_executable_ignores_configured_override(monkeypatch):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import (
        resolve_python_executable,
    )

    monkeypatch.setenv("DEEPRESEARCH_PYTHON_EXECUTABLE", "/ambient/must-not-win")
    assert resolve_python_executable() == Path(os.path.abspath(sys.executable))


def test_resolve_python_executable_preserves_symlinked_venv_identity(
    tmp_path: Path,
    monkeypatch,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import (
        build_child_env,
        resolve_python_executable,
    )

    venv_root = tmp_path / "symlink-venv"
    bin_dir = venv_root / "bin"
    bin_dir.mkdir(parents=True)
    (venv_root / "pyvenv.cfg").write_text("home = /usr/bin\n", encoding="utf-8")
    python = bin_dir / "python"
    try:
        python.symlink_to(sys.executable)
    except OSError:
        # Creating symlinks needs privilege on Windows; the resolution
        # semantics are covered where symlinks are creatable.
        pytest.skip("symlink creation requires privilege on this platform")
    monkeypatch.setattr(sys, "executable", str(python))
    resolved = resolve_python_executable()
    child_env = build_child_env(resolved)

    assert resolved == python
    assert child_env["VIRTUAL_ENV"] == str(venv_root)
    assert child_env["PATH"].split(os.pathsep)[0] == str(bin_dir)


def test_resolve_python_executable_accepts_current_process_runtime_without_virtualenv(
    tmp_path: Path,
    monkeypatch,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import (
        build_child_env,
        resolve_python_executable,
    )

    python = tmp_path / "embedded-runtime" / "python"
    python.parent.mkdir(parents=True)
    python.write_text("#!/bin/sh\n", encoding="utf-8")
    python.chmod(0o755)
    monkeypatch.setattr(sys, "executable", str(python))
    monkeypatch.delenv("DEEPRESEARCH_PYTHON_EXECUTABLE", raising=False)
    resolved = resolve_python_executable()
    child_env = build_child_env(resolved)

    assert resolved == python
    assert "VIRTUAL_ENV" not in child_env
    assert child_env["PATH"].split(os.pathsep)[0] == str(python.parent)


@pytest.mark.parametrize("invalid_kind", ["relative", "missing", "not_executable"])
def test_resolve_python_executable_rejects_invalid_current_runtime(
    tmp_path: Path,
    invalid_kind: str,
    monkeypatch,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import (
        DeepResearchRuntimeError,
        resolve_python_executable,
    )

    if invalid_kind == "relative":
        configured = "relative/bin/python"
    elif invalid_kind == "missing":
        configured = str(tmp_path / "missing" / "python")
    elif invalid_kind == "not_executable":
        if os.name == "nt":
            pytest.skip("execute bits are not materialized on Windows")
        configured = str(_fake_venv_python(tmp_path, executable=False))

    monkeypatch.setattr(sys, "executable", configured)
    with pytest.raises(DeepResearchRuntimeError, match="^runtime_python_invalid$"):
        resolve_python_executable()


def test_build_child_env_isolates_python_and_allows_only_http_proxy_family(
    tmp_path: Path,
    monkeypatch,
):
    from jiuwenswarm.agents.harness.common.tools.deepresearch.runtime import build_child_env

    if os.name == "nt":
        pytest.skip("env var case distinction is unavailable on Windows")
    python = _fake_venv_python(tmp_path)
    process_proxies = {
        "HTTP_PROXY": "http://process-upper-http",
        "http_proxy": "http://process-lower-http",
        "HTTPS_PROXY": "http://process-upper-https",
        "https_proxy": "http://process-lower-https",
        "NO_PROXY": "process-upper.internal",
        "no_proxy": "process-lower.internal",
    }
    monkeypatch.setenv("PATH", "/process/bin")
    monkeypatch.setenv("PYTHONHOME", "/parent/python-home")
    monkeypatch.setenv("PYTHONPATH", "/parent/python-path")
    monkeypatch.setenv("ALL_PROXY", "socks5://forbidden-upper")
    monkeypatch.setenv("all_proxy", "socks5://forbidden-lower")
    for key, value in process_proxies.items():
        monkeypatch.setenv(key, value)
    token = bind_task_env_overlay(
        {
            "HTTP_PROXY": "http://tenant-upper-http",
            "http_proxy": "http://tenant-lower-http",
            "HTTPS_PROXY": "http://tenant-upper-https",
            "https_proxy": "http://tenant-lower-https",
            "NO_PROXY": "tenant-upper.internal",
            "no_proxy": "tenant-lower.internal",
            "ALL_PROXY": "socks5://tenant-forbidden-upper",
            "all_proxy": "socks5://tenant-forbidden-lower",
            "LLM_API_KEY": "tenant-secret",
        }
    )
    try:
        child_env = build_child_env(python)
    finally:
        reset_task_env_overlay(token)

    assert child_env["VIRTUAL_ENV"] == str(python.parent.parent)
    assert child_env["PATH"].split(os.pathsep)[0] == str(python.parent)
    assert child_env["PATH"].split(os.pathsep)[1:] == ["/process/bin"]
    assert {key: child_env[key] for key in process_proxies} == process_proxies
    for forbidden in ("PYTHONHOME", "PYTHONPATH", "ALL_PROXY", "all_proxy", "LLM_API_KEY"):
        assert forbidden not in child_env
