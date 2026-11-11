# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""CLI error reporting for a damaged ``instances.yaml``.

``load_instances_yaml()`` raises ``InstancesYamlError`` with a user-facing
message and fix suggestions ("for CLI display"). The instance commands must
print that message and exit non-zero instead of leaking a traceback.
"""

from __future__ import annotations

import sys

import pytest

from jiuwenswarm.common import utils as common_utils


def _prepare_broken_home(tmp_path, monkeypatch) -> None:
    monkeypatch.delenv("JIUWENSWARM_DATA_DIR", raising=False)
    monkeypatch.setenv("JIUWENSWARM_HOME", str(tmp_path))
    monkeypatch.setenv("JIUWENSWARM_SKIP_DOTENV", "1")
    monkeypatch.setattr(common_utils, "_user_home", None, raising=False)
    monkeypatch.setattr(common_utils, "_workspace_base_dir", None, raising=False)
    config_dir = tmp_path / ".jiuwenswarm"
    config_dir.mkdir()
    (config_dir / "instances.yaml").write_text(
        "instances:\n  alice: [unclosed\n", encoding="utf-8",
    )


@pytest.mark.parametrize("action", [
    ("--status", "alice"),
    ("--stop", "alice"),
    ("--restart", "alice"),
    ("--name", "alice"),
])
def test_start_reports_instances_yaml_error_without_traceback(
    tmp_path, monkeypatch, caplog, action,
):
    _prepare_broken_home(tmp_path, monkeypatch)
    from jiuwenswarm import start_services

    monkeypatch.setattr(sys, "argv", ["jiuwenswarm-start", *action])

    with caplog.at_level("INFO"), pytest.raises(SystemExit) as excinfo:
        start_services.main()

    assert excinfo.value.code == 1
    assert "[instances.yaml]" in caplog.text


def test_init_named_instance_reports_instances_yaml_error(tmp_path, monkeypatch, caplog):
    _prepare_broken_home(tmp_path, monkeypatch)
    from jiuwenswarm.init_workspace import run_init

    with caplog.at_level("INFO"):
        rc = run_init(force=False, name="alice")

    assert rc == 1
    assert "[instances.yaml]" in caplog.text
    assert not (tmp_path / ".jiuwenswarm-instances" / "alice").exists()
