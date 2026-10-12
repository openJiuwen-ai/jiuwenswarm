# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Bundled ripgrep self-check: only a binary that runs is handed to agent-core.

The fakes are real executables run through a real subprocess, including one
that kills itself with SIGKILL the way macOS kills a quarantined binary.
"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from jiuwenswarm.common import bundled_ripgrep as br

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX shell fakes")


def _fake_rg(bundle_root: Path, body: str) -> Path:
    path = bundle_root.joinpath(*br.BUNDLE_SUBDIR, br.bundled_rg_name())
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\n" + body + "\n", encoding="utf-8")
    path.chmod(0o755)
    return path


def _working(bundle_root: Path, marker: Path | None = None) -> Path:
    touch = f'touch "{marker}"\n' if marker else ""
    return _fake_rg(bundle_root, touch + 'echo "ripgrep 15.2.0 (rev e89fff89ac)"')


def test_working_binary_is_handed_to_agent_core(tmp_path: Path) -> None:
    rg = _working(tmp_path)
    env: dict[str, str] = {}

    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) == str(rg)
    assert env["OPENJIUWEN_RG"] == str(rg)
    assert env[br.STATUS_ENV] == "ok:ripgrep 15.2.0 (rev e89fff89ac)"


def test_binary_killed_on_launch_is_not_used(tmp_path: Path, caplog) -> None:
    # What macOS does to a quarantined / badly signed binary: SIGKILL, exit 137.
    _fake_rg(tmp_path, "kill -9 $$")
    env: dict[str, str] = {}

    # The jiuwenswarm logger does not propagate to root once configured, so
    # attach the capture handler to the emitting logger itself.
    br.LOGGER.addHandler(caplog.handler)
    try:
        with caplog.at_level(logging.WARNING, logger=br.LOGGER.name):
            assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) is None
    finally:
        br.LOGGER.removeHandler(caplog.handler)

    assert "OPENJIUWEN_RG" not in env
    assert env[br.STATUS_ENV].startswith("unusable:killed by signal 9")
    assert "unusable" in caplog.text


def test_hanging_binary_times_out_and_is_not_used(tmp_path: Path) -> None:
    _fake_rg(tmp_path, "sleep 5")
    env: dict[str, str] = {}

    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path, timeout=0.3) is None
    assert "OPENJIUWEN_RG" not in env
    assert "did not finish" in env[br.STATUS_ENV]


def test_binary_that_is_not_ripgrep_is_not_used(tmp_path: Path) -> None:
    _fake_rg(tmp_path, 'echo "something else"')
    env: dict[str, str] = {}

    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) is None
    assert env[br.STATUS_ENV].startswith("unusable:exit code 0")


def test_missing_binary_records_absent(tmp_path: Path) -> None:
    env: dict[str, str] = {}

    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) is None
    assert env == {br.STATUS_ENV: "absent"}


@pytest.mark.parametrize("key", br.RG_ENV_KEYS)
def test_user_setting_wins_and_the_bundle_is_not_even_run(
    tmp_path: Path, key: str
) -> None:
    marker = tmp_path / "ran"
    _working(tmp_path, marker)
    env = {key: "/opt/custom/rg"}

    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) is None
    assert env.get("OPENJIUWEN_RG", "") == (
        "/opt/custom/rg" if key == "OPENJIUWEN_RG" else ""
    )
    assert env[br.STATUS_ENV] == f"user:{key}"
    assert not marker.exists()


def test_child_process_trusts_the_recorded_outcome(tmp_path: Path) -> None:
    marker = tmp_path / "ran"
    _working(tmp_path, marker)
    env = {br.STATUS_ENV: "unusable:killed by signal 9"}

    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) is None
    assert "OPENJIUWEN_RG" not in env
    assert not marker.exists()


def test_source_runs_have_no_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delattr(sys, "frozen", raising=False)
    env: dict[str, str] = {}

    assert br.frozen_bundle_root() is None
    assert br.configure_bundled_ripgrep(env) is None
    assert env == {}


def test_frozen_root_is_meipass(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(sys, "frozen", True, raising=False)
    monkeypatch.setattr(sys, "_MEIPASS", str(tmp_path), raising=False)
    rg = _working(tmp_path)

    assert br.frozen_bundle_root() == tmp_path
    assert br.bundled_rg_path() == rg


def test_agent_core_resolver_uses_the_configured_binary(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    resolver = pytest.importorskip("openjiuwen.harness.tools.rg_binary")
    rg = _working(tmp_path)
    env: dict[str, str] = {}
    assert br.configure_bundled_ripgrep(env, bundle_root=tmp_path) == str(rg)

    # The child processes inherit exactly this environment variable.
    monkeypatch.setenv("OPENJIUWEN_RG", env["OPENJIUWEN_RG"])
    monkeypatch.delenv("ICODE_RG", raising=False)
    assert Path(resolver.resolve_rg_binary()).resolve() == rg.resolve()


def test_status_report_logs_each_outcome(caplog) -> None:
    log = logging.getLogger("test.bundled_ripgrep")
    cases = {
        "ok:ripgrep 15.2.0": ("INFO", "using bundled ripgrep 15.2.0"),
        "unusable:killed by signal 9": ("WARNING", "killed by signal 9"),
        "absent": ("INFO", "no bundled ripgrep"),
    }
    for status, (level, text) in cases.items():
        caplog.clear()
        with caplog.at_level(logging.INFO, logger=log.name):
            assert (
                br.report_bundled_ripgrep_status(log, {br.STATUS_ENV: status}) == status
            )
        assert caplog.records[-1].levelname == level
        assert text in caplog.text
