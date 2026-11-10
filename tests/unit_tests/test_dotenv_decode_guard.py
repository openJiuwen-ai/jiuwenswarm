# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
from __future__ import annotations

import logging
import os
import runpy
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from dotenv import dotenv_values, load_dotenv
from dotenv.main import DotEnv

from jiuwenswarm import dotenv_early
from jiuwenswarm.instance_manager import bootstrap as im_bootstrap

GBK_ENV = "# 远程调试端口\nISSUE12_GUARD_KEY=from-gbk\n".encode("gbk")


def _clear_key(monkeypatch) -> None:
    # setenv first so teardown restores the key to "absent" even after dotenv sets it
    monkeypatch.setenv("ISSUE12_GUARD_KEY", "")
    monkeypatch.delenv("ISSUE12_GUARD_KEY")


def _write_caller(root: Path) -> Path:
    caller = root / "pkg" / "settings.py"
    caller.parent.mkdir(parents=True)
    caller.write_text("from dotenv import load_dotenv\nRESULT = load_dotenv()\n", encoding="utf-8")
    return caller


def _implicit_setup(tmp_path: Path, monkeypatch, env_bytes: bytes) -> Path:
    _clear_key(monkeypatch)
    monkeypatch.setattr(sys, "gettrace", lambda: None)
    (tmp_path / ".env").write_bytes(env_bytes)
    caller = _write_caller(tmp_path)
    elsewhere = tmp_path / "cwd"
    elsewhere.mkdir()
    monkeypatch.chdir(elsewhere)
    return caller


def test_third_party_explicit_non_utf8_env_is_skipped(tmp_path: Path, monkeypatch, caplog):
    _clear_key(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_bytes(GBK_ENV)
    # setup_logger 把 jiuwenswarm 命名空间置 propagate=False，caplog 只挂 root；直接挂到模块 logger 上
    early_logger = logging.getLogger("jiuwenswarm.early")
    early_logger.addHandler(caplog.handler)
    try:
        assert load_dotenv(dotenv_path=env_file) is False
    finally:
        early_logger.removeHandler(caplog.handler)
    assert "ISSUE12_GUARD_KEY" not in os.environ
    assert "skip non-UTF-8 env file" in caplog.text


def test_third_party_dotenv_values_non_utf8_returns_empty(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_bytes(GBK_ENV)
    assert dict(dotenv_values(env_file)) == {}


def test_third_party_implicit_find_dotenv_non_utf8_does_not_raise(tmp_path: Path, monkeypatch):
    caller = _implicit_setup(tmp_path, monkeypatch, GBK_ENV)
    assert runpy.run_path(str(caller))["RESULT"] is False
    assert "ISSUE12_GUARD_KEY" not in os.environ


def test_implicit_find_dotenv_still_resolves_from_caller_file(tmp_path: Path, monkeypatch):
    caller = _implicit_setup(tmp_path, monkeypatch, b"ISSUE12_GUARD_KEY=from-utf8\n")
    assert runpy.run_path(str(caller))["RESULT"] is True
    assert os.environ["ISSUE12_GUARD_KEY"] == "from-utf8"


def test_jiuwenswarm_implicit_find_dotenv_non_utf8_does_not_raise(tmp_path: Path, monkeypatch):
    # image_tools.py 在 jiuwenswarm 模块内无参 load_dotenv()，同样会命中安装根 .env
    caller = _implicit_setup(tmp_path, monkeypatch, GBK_ENV)
    assert runpy.run_path(str(caller), run_name="jiuwenswarm._issue12_probe")["RESULT"] is False
    assert "ISSUE12_GUARD_KEY" not in os.environ


def test_load_dotenv_runtime_non_utf8_still_raises(tmp_path: Path, monkeypatch):
    _clear_key(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_bytes(GBK_ENV)
    with pytest.raises(UnicodeDecodeError):
        dotenv_early.load_dotenv_runtime(env_file, override=True)
    assert "ISSUE12_GUARD_KEY" not in os.environ


def test_parse_dotenv_early_non_utf8_fails_closed(tmp_path: Path, monkeypatch):
    _clear_key(monkeypatch)
    env_file = tmp_path / "bootstrap.env"
    env_file.write_bytes(GBK_ENV)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-agentserver", "--dotenv", str(env_file)])
    monkeypatch.setattr(dotenv_early, "_component_name", "jiuwenswarm")
    monkeypatch.setattr(dotenv_early, "_parsed_dotenv", None)
    with pytest.raises(UnicodeDecodeError):
        dotenv_early.parse_dotenv_early("jiuwenswarm-agentserver")
    assert dotenv_early.get_parsed_dotenv() is None


def test_load_instance_bootstrap_non_utf8_fails_closed(tmp_path: Path, monkeypatch):
    _clear_key(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_bytes(GBK_ENV)
    config = SimpleNamespace(workspace=tmp_path, get_bootstrap_env_path=lambda: env_file)
    monkeypatch.setattr(im_bootstrap, "get_instance_config", lambda name: config)
    with pytest.raises(UnicodeDecodeError):
        im_bootstrap.load_instance_bootstrap_by_name("alice")
    assert "ISSUE12_GUARD_KEY" not in os.environ


def test_load_dotenv_runtime_missing_file_still_returns_false(tmp_path: Path):
    assert dotenv_early.load_dotenv_runtime(tmp_path / "absent.env", override=True) is False


def test_cli_main_non_utf8_config_env_fails_closed(tmp_path: Path, monkeypatch):
    from jiuwenswarm.cli import main as cli_main
    from jiuwenswarm.common import utils as common_utils
    _clear_key(monkeypatch)
    env_file = tmp_path / ".env"
    env_file.write_bytes(GBK_ENV)
    monkeypatch.delenv("JIUWENSWARM_SKIP_DOTENV", raising=False)
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm"])
    monkeypatch.setattr(common_utils, "get_env_file", lambda: env_file)
    with pytest.raises(UnicodeDecodeError):
        cli_main.main()
    assert "ISSUE12_GUARD_KEY" not in os.environ


def test_ensure_utf8_dotenv_accepts_utf8_and_missing(tmp_path: Path):
    env_file = tmp_path / ".env"
    env_file.write_text("# 中文注释\nISSUE12_GUARD_KEY=ok\n", encoding="utf-8")
    dotenv_early.ensure_utf8_dotenv(env_file)
    dotenv_early.ensure_utf8_dotenv(tmp_path / "absent.env")
    dotenv_early.ensure_utf8_dotenv(None)


def test_guard_install_is_idempotent():
    installed = DotEnv.dict
    dotenv_early._install_dotenv_decode_guard()
    assert DotEnv.dict is installed
    assert getattr(DotEnv.dict, "jiuwenswarm_decode_guard", False)
