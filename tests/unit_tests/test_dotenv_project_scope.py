# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for project-scoped ``--dotenv`` (A13).

The framework advertises ``--dotenv <path>`` for "project/instance scoped
configuration without moving the data directory", but two gaps made the flag
useless for the *default* instance:

* ``jiuwenswarm.common.utils.get_env_file()`` always returned
  ``<config dir>/.env``, and ``jiuwenswarm.app`` runs
  ``load_dotenv_runtime(get_env_file(), override=True)`` **after** the early
  ``parse_dotenv_early()``. The shared ``~/.jiuwenswarm/config/.env`` therefore
  overwrote every value the explicit file had just loaded.
* ``jiuwenswarm-start app --dotenv <path>`` was rejected by argparse
  ("unrecognized arguments"), and ``_run()`` never forwarded the flag to the
  ``app`` / ``web`` child processes anyway — each child re-parses its own
  ``sys.argv``, so a flag that is not forwarded simply does not exist.

These tests pin the three behaviours separately, so a future refactor that
drops any one of them turns red for that specific reason:

1. the runtime ``.env`` source is the file named by ``--dotenv``;
2. values from that file survive the runtime load (they are not re-overwritten);
3. the flag is accepted by argparse and forwarded to every child process.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from jiuwenswarm import dotenv_early, start_services
from jiuwenswarm.common import utils
from jiuwenswarm.common.utils import get_config_dir, get_env_file


@pytest.fixture(autouse=True)
def _isolate_early_parse(monkeypatch: pytest.MonkeyPatch):
    """Keep the early-parse global from leaking between tests."""
    monkeypatch.setattr(dotenv_early, "_parsed_dotenv", None, raising=False)


def _launch_with(monkeypatch: pytest.MonkeyPatch, argv: list[str]) -> None:
    """Replay the import-time early parse for one launch command line."""
    monkeypatch.setattr(sys, "argv", list(argv))
    dotenv_early.parse_dotenv_early("test")


@pytest.fixture
def project_env(tmp_path: Path) -> Path:
    env_file = tmp_path / "project.env"
    env_file.write_text(
        'HELIX_PROJECT_KEY="sk-project-scoped"\n'
        'HELIX_PROJECT_NAME="proj-a"\n',
        encoding="utf-8",
    )
    return env_file.resolve()


# -- 1. the runtime .env source ------------------------------------------------


def test_get_env_file_returns_the_explicit_dotenv(
    project_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launch_with(
        monkeypatch,
        ["jiuwenswarm-start", "app", "--dotenv", str(project_env)],
    )

    assert get_env_file() == project_env, (
        "runtime load must read the --dotenv file, otherwise the shared "
        "config/.env overwrites the project configuration"
    )


def test_shared_config_still_wins_when_no_dotenv_is_given(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    _launch_with(monkeypatch, ["jiuwenswarm-start", "app"])

    assert get_env_file() == get_config_dir() / ".env"


def test_missing_dotenv_file_falls_back_to_the_shared_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    missing = tmp_path / "does-not-exist.env"
    _launch_with(monkeypatch, ["jiuwenswarm-start", "app", "--dotenv", str(missing)])

    assert dotenv_early.get_parsed_dotenv() is None
    assert get_env_file() == get_config_dir() / ".env", (
        "an unreadable --dotenv must not break startup"
    )


# -- 2. the values survive the runtime load ------------------------------------


def test_project_values_are_loaded_and_not_overwritten(
    project_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Pre-register the keys so monkeypatch removes them again after the test:
    # load_dotenv_runtime() writes straight into os.environ.
    monkeypatch.setenv("HELIX_PROJECT_KEY", "")
    monkeypatch.setenv("HELIX_PROJECT_NAME", "")

    _launch_with(
        monkeypatch,
        ["jiuwenswarm-start", "app", "--dotenv", str(project_env)],
    )

    assert os.environ["HELIX_PROJECT_KEY"] == "sk-project-scoped"
    assert os.environ["HELIX_PROJECT_NAME"] == "proj-a"


def test_shared_config_cannot_overwrite_the_project_file(
    project_env: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The reported failure mode, reproduced end to end.

    ``jiuwenswarm.app`` re-loads the runtime env *after* the early parse::

        load_dotenv_runtime(get_env_file(), override=True)

    When ``get_env_file()`` points at the shared ``<config dir>/.env`` that
    load wins, so a shared key silently overwrites the project's — two
    projects on one machine end up billed and attributed to the wrong one.
    """
    shared_config = tmp_path / "shared-config"
    shared_config.mkdir()
    (shared_config / ".env").write_text(
        'HELIX_PROJECT_KEY="sk-shared-other-project"\n', encoding="utf-8"
    )
    monkeypatch.setattr(utils, "get_config_dir", lambda: shared_config)
    monkeypatch.setenv("HELIX_PROJECT_KEY", "")

    _launch_with(
        monkeypatch,
        ["jiuwenswarm-start", "app", "--dotenv", str(project_env)],
    )
    assert get_env_file() == project_env

    # The app's runtime load, verbatim from jiuwenswarm/app.py.
    from jiuwenswarm.dotenv_early import load_dotenv_runtime

    load_dotenv_runtime(get_env_file(), override=True)

    assert os.environ["HELIX_PROJECT_KEY"] == "sk-project-scoped", (
        "the shared config overwrote the project-scoped value"
    )


# -- 3. argparse and child-process forwarding ----------------------------------


def test_dotenv_flag_is_accepted_by_the_cli(
    project_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _launch_with(
        monkeypatch,
        ["jiuwenswarm-start", "app", "--dotenv", str(project_env)],
    )

    args = start_services._parse_args()

    assert args.mode == "app"
    assert args.dotenv == str(project_env), (
        "`jiuwenswarm-start app --dotenv X` used to fail in argparse with "
        "'unrecognized arguments'"
    )


def test_run_forwards_the_dotenv_flag_to_every_child(
    project_env: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The app/web children must receive the same ``--dotenv``."""
    _launch_with(
        monkeypatch,
        ["jiuwenswarm-start", "app", "--dotenv", str(project_env)],
    )

    class _FakeInstanceCommand:
        def __init__(self, name: str) -> None:
            self.name = name
            self.config = type("Cfg", (), {"ports": {}})()

        def validate_and_load(self) -> int | None:
            return None

        def check_ports_conflicts(self) -> bool:
            return False

    captured: dict[str, object] = {}

    def _fake_run_processes(commands, ports):
        captured["commands"] = commands
        return 0

    monkeypatch.setattr(start_services, "InstanceCommand", _FakeInstanceCommand)
    monkeypatch.setattr(start_services, "_sync_default_env_ports", lambda ports: None)
    monkeypatch.setattr(start_services, "_run_processes", _fake_run_processes)

    assert start_services._run("app") == 0

    commands = captured["commands"]
    assert commands, "app mode must produce at least one child command"
    for name, cmd, _cwd in commands:
        assert "--dotenv" in cmd, f"{name} child lost --dotenv"
        assert cmd[cmd.index("--dotenv") + 1] == str(project_env)


def test_build_commands_keeps_the_flag_out_when_not_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No flag on the command line => no flag in the children (opt-in only)."""
    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-start", "app"])
    dotenv_early.parse_dotenv_early("test")

    for _name, cmd, _cwd in start_services._build_commands("app"):
        assert "--dotenv" not in cmd
