"""Regression tests for pre-release aware install arguments.

The update check resolves pre-release targets (beta channel), but pip and uv
skip pre-releases by default, so an upgrade offered for ``0.2.9b3`` used to
finish with "Requirement already satisfied" and no change. The installer must
opt in only when the offered release is itself a pre-release.
"""

from jiuwenswarm.common.upgrade_executor import PipExecutor


def _noop_callback(_: dict) -> None:
    return None


def _build(monkeypatch, latest_version: str, uv_cmd: str | None):
    monkeypatch.setattr(
        PipExecutor, "_resolve_uv_command", staticmethod(lambda: uv_cmd)
    )
    executor = PipExecutor(
        {"pypi_mirror": "", "latest_version": latest_version},
        _noop_callback,
    )
    return executor._build_install_args("workswarm", 20)


def test_pip_allows_prerelease_for_beta_target(monkeypatch):
    args = _build(monkeypatch, "0.2.9b3", uv_cmd=None)

    assert "--pre" in args
    assert "workswarm" in args


def test_pip_keeps_stable_targets_unchanged(monkeypatch):
    args = _build(monkeypatch, "0.2.9", uv_cmd=None)

    assert "--pre" not in args


def test_uv_allows_prerelease_for_beta_target(monkeypatch):
    args = _build(monkeypatch, "0.2.9b3", uv_cmd="/usr/bin/uv")

    index = args.index("--prerelease")
    assert args[index + 1] == "allow"


def test_uv_keeps_stable_targets_unchanged(monkeypatch):
    args = _build(monkeypatch, "0.2.8", uv_cmd="/usr/bin/uv")

    assert "--prerelease" not in args


def test_empty_latest_version_keeps_defaults(monkeypatch):
    pip_args = _build(monkeypatch, "", uv_cmd=None)
    uv_args = _build(monkeypatch, "", uv_cmd="/usr/bin/uv")

    assert "--pre" not in pip_args
    assert "--prerelease" not in uv_args
