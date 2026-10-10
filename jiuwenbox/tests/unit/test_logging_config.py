"""Runner logging must work without server dependencies."""

import os
import subprocess
import sys
from pathlib import Path

import pytest

from jiuwenbox import logging_config


def test_runner_imports_without_site_packages():
    env = os.environ.copy()
    env.pop("PYTHONHOME", None)
    env["PYTHONPATH"] = str(Path(__file__).resolve().parents[2] / "src")
    proc = subprocess.run(
        [sys.executable, "-S", "-c",
         "import importlib.util; assert importlib.util.find_spec('uvicorn') is None; "
         "from jiuwenbox.supervisor import win_exec, win_job, win_softdelete, win_acl, win_setup"],
        env=env, capture_output=True, text=True, timeout=15,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
    )
    assert proc.returncode == 0, proc.stderr


def test_uvicorn_internal_import_error_is_not_suppressed(monkeypatch):
    import builtins

    original_import = builtins.__import__

    def broken_import(name, *args, **kwargs):
        if name == "uvicorn.config":
            raise ModuleNotFoundError("missing uvicorn dependency", name="click")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", broken_import)
    with pytest.raises(ModuleNotFoundError, match="missing uvicorn dependency"):
        logging_config.configure_logging()


def test_uvicorn_logging_patch_is_preserved():
    from uvicorn.config import LOGGING_CONFIG

    logging_config.configure_logging()
    assert LOGGING_CONFIG["formatters"]["default"]["fmt"] == logging_config.LOG_FORMAT
    assert LOGGING_CONFIG["formatters"]["default"]["datefmt"] == logging_config.LOG_DATE_FORMAT
