# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Gateway-owned dotenv loader: read an existing file, never write one."""

from __future__ import annotations

import ast
import os
import sys
from pathlib import Path

import gateway.dotenv_early as dotenv_early
from gateway.dotenv_early import (
    CLI_PORTS_ENV_FLAG,
    get_parsed_dotenv,
    load_dotenv_runtime,
    parse_dotenv_early,
)


def _argv(monkeypatch, *args: str) -> None:
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-gateway", *args])


def test_copy_does_not_reach_instance_manager() -> None:
    source = Path(dotenv_early.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any("instance_manager" in name for name in imported)
    assert "_create_basic_bootstrap_env" not in source


def test_dotenv_flag_loads_data_dir_and_keeps_argv(tmp_path: Path, monkeypatch) -> None:
    env_file = tmp_path / "custom.env"
    data_dir = tmp_path / "data"
    env_file.write_text(f"JIUWENSWARM_DATA_DIR={data_dir}\n", encoding="utf-8")
    argv = ["jiuwenswarm-gateway", "--dotenv", str(env_file), "--name", "ignored"]
    monkeypatch.setattr(sys, "argv", list(argv))
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)

    loaded = parse_dotenv_early("jiuwenswarm-gateway")

    assert loaded == env_file.resolve()
    assert get_parsed_dotenv() == env_file.resolve()
    assert os.environ["JIUWENSWARM_DATA_DIR"] == str(data_dir)
    assert sys.argv == argv


def test_missing_dotenv_file_returns_none(tmp_path: Path, monkeypatch) -> None:
    missing = tmp_path / "missing.env"
    _argv(monkeypatch, "--dotenv", str(missing))

    assert parse_dotenv_early("jiuwenswarm-gateway") is None
    assert get_parsed_dotenv() is None
    assert not missing.exists()


def test_name_loads_workspace_line(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    data_dir = tmp_path / "instance-data"
    (workspace / ".env").write_text(
        f"JIUWENSWARM_DATA_DIR={data_dir}\nWEB_PORT=19000\n",
        encoding="utf-8",
    )
    yaml_dir = home / ".jiuwenswarm"
    yaml_dir.mkdir(parents=True)
    (yaml_dir / "instances.yaml").write_text(
        "instances:\n"
        "  demo:\n"
        f'    workspace: "{workspace}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_HOME", str(home))
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    _argv(monkeypatch, "--name", "demo")

    loaded = parse_dotenv_early("jiuwenswarm-gateway")

    assert loaded == (workspace / ".env").resolve()
    assert os.environ["JIUWENSWARM_DATA_DIR"] == str(data_dir)
    assert os.environ["WEB_PORT"] == "19000"


def test_name_without_workspace_field_uses_instances_dir(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = home / ".jiuwenswarm-instances" / "demo"
    workspace.mkdir(parents=True)
    data_dir = tmp_path / "default-layout"
    (workspace / ".env").write_text(
        f"JIUWENSWARM_DATA_DIR={data_dir}\n",
        encoding="utf-8",
    )
    yaml_dir = home / ".jiuwenswarm"
    yaml_dir.mkdir()
    (yaml_dir / "instances.yaml").write_text(
        "instances:\n  demo: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_HOME", str(home))
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    _argv(monkeypatch, "--name", "demo")

    loaded = parse_dotenv_early("jiuwenswarm-gateway")

    assert loaded == (workspace / ".env").resolve()
    assert os.environ["JIUWENSWARM_DATA_DIR"] == str(data_dir)


def test_name_missing_env_does_not_create_file(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    yaml_dir = home / ".jiuwenswarm"
    yaml_dir.mkdir(parents=True)
    (yaml_dir / "instances.yaml").write_text(
        "instances:\n"
        "  demo:\n"
        f'    workspace: "{workspace}"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_HOME", str(home))
    _argv(monkeypatch, "--name", "demo")
    bootstrap_env = workspace / ".env"

    assert parse_dotenv_early("jiuwenswarm-gateway") is None
    assert get_parsed_dotenv() is None
    assert not bootstrap_env.exists()


def test_name_rejects_reserved_and_unknown(tmp_path: Path, monkeypatch) -> None:
    home = tmp_path / "home"
    yaml_dir = home / ".jiuwenswarm"
    yaml_dir.mkdir(parents=True)
    (yaml_dir / "instances.yaml").write_text(
        "instances:\n  demo: {}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_HOME", str(home))

    _argv(monkeypatch, "--name", "default")
    assert parse_dotenv_early("jiuwenswarm-gateway") is None

    _argv(monkeypatch, "--name", "missing")
    assert parse_dotenv_early("jiuwenswarm-gateway") is None

    monkeypatch.delenv("JIUWENSWARM_HOME", raising=False)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path / "empty-home"))
    _argv(monkeypatch, "--name", "demo")
    assert parse_dotenv_early("jiuwenswarm-gateway") is None


def test_desktop_ports_survive_override_and_url_is_dropped(
    tmp_path: Path, monkeypatch,
) -> None:
    env_file = tmp_path / ".env"
    env_file.write_text(
        "WEB_PORT=11111\nGATEWAY_PORT=22222\n"
        "AGENT_SERVER_URL=ws://127.0.0.1:18092\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.delenv(CLI_PORTS_ENV_FLAG, raising=False)
    monkeypatch.setenv("WEB_PORT", "20000")
    monkeypatch.setenv("GATEWAY_PORT", "20001")
    monkeypatch.setenv("AGENT_SERVER_URL", "ws://127.0.0.1:1")

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["WEB_PORT"] == "20000"
    assert os.environ["GATEWAY_PORT"] == "20001"
    assert "AGENT_SERVER_URL" not in os.environ
