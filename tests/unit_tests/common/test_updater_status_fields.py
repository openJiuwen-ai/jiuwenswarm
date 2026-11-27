"""UpdateStatus must serialize the executor's progress fields.

The pip upgrade executor pushes ``current_activity``/``pip_output`` through
``UpdaterService._executor_callback`` -> ``_update_status`` (setattr). They were
missing from the ``UpdateStatus`` dataclass, so ``asdict`` silently dropped
them and the UpdatePanel frontend read perpetual blanks.
"""

import logging

from jiuwenswarm.common import updater
from jiuwenswarm.common.upgrade_executor import PipExecutor


def _make_service(monkeypatch) -> updater.UpdaterService:
    monkeypatch.setattr(updater, "get_config_raw", lambda: {"updater": {"enabled": True}})
    return updater.UpdaterService()


def test_status_serializes_progress_fields(monkeypatch):
    service = _make_service(monkeypatch)
    service._update_status(  # pylint: disable=protected-access
        state="upgrading",
        current_activity="Collecting workswarm",
        pip_output="Looking in indexes: ...",
    )
    status = service.get_status()
    assert status["current_activity"] == "Collecting workswarm"
    assert status["pip_output"] == "Looking in indexes: ..."


def test_executor_callback_routes_progress_fields(monkeypatch):
    service = _make_service(monkeypatch)
    service._executor_callback(  # pylint: disable=protected-access
        {
            "state": "upgrading",
            "downloaded_bytes": 42,
            "total_bytes": 100,
            "current_activity": "Downloading workswarm-1.0.0",
            "error": "",
        }
    )
    status = service.get_status()
    assert status["current_activity"] == "Downloading workswarm-1.0.0"
    assert status["downloaded_bytes"] == 42


def test_default_status_has_empty_progress_fields(monkeypatch):
    service = _make_service(monkeypatch)
    status = service.get_status()
    assert status["current_activity"] == ""
    assert status["pip_output"] == ""


def test_update_status_ignores_undeclared_fields(monkeypatch, caplog):
    """_update_status must not accept keys missing from the dataclass.

    asdict() silently drops undeclared attributes, so accepting them here
    would re-create the bug this module regresses against: a pushed field
    that never reaches get_status().
    """
    service = _make_service(monkeypatch)
    with caplog.at_level(logging.DEBUG, logger="jiuwenswarm"):
        service._update_status(  # pylint: disable=protected-access
            current_activity="real line",
            not_a_status_field="value",
        )
    status = service.get_status()
    assert status["current_activity"] == "real line"
    assert "not_a_status_field" not in status
    assert any("not_a_status_field" in r.message for r in caplog.records)


def test_start_download_resets_leftover_pip_activity(monkeypatch):
    """A failed previous run must not flash its activity line on restart.

    start_download enters "upgrading" and resets the pip progress fields
    before the executor thread pushes its first callback.
    """
    service = _make_service(monkeypatch)
    service._update_status(  # pylint: disable=protected-access
        state="update_available",
        has_update=True,
        current_activity="stale pip line from failed run",
    )

    def fake_create_executor(_mode, _config, callback):
        def install():
            callback({"state": "upgrading", "current_activity": "", "error": ""})

        executor = install
        executor.install = install
        return executor

    monkeypatch.setattr(updater, "create_executor", fake_create_executor)
    status = service.start_download()
    assert status["state"] == "upgrading"
    assert status["current_activity"] == ""


class _FakePopen:
    """Stands in for subprocess.Popen inside PipExecutor.install."""

    def __init__(self, lines, returncode=0):
        self._lines = lines
        self.returncode = returncode
        self.stdout = self

    def __iter__(self):
        return iter(self._lines)

    def readline(self):
        return self._lines.pop(0) if self._lines else ""

    def wait(self):
        return None


def _run_pip_executor(monkeypatch, lines, returncode=0):
    pushes = []
    monkeypatch.setattr(
        PipExecutor, "_check_editable_install", lambda self, package: None
    )
    monkeypatch.setattr(PipExecutor, "_resolve_uv_command", lambda self: None)
    monkeypatch.setattr(
        "jiuwenswarm.common.upgrade_executor.subprocess.Popen",
        lambda *args, **kwargs: _FakePopen(list(lines), returncode),
    )
    executor = PipExecutor({"timeout_seconds": 20}, pushes.append)
    executor.install()
    return pushes


def test_pip_executor_success_clears_activity_and_pushes_output(monkeypatch):
    pushes = _run_pip_executor(
        monkeypatch,
        ["Looking in indexes: ...", "Collecting workswarm", "Successfully installed workswarm-1.2.3"],
    )
    assert pushes[0]["current_activity"] == ""
    activity_pushes = [p for p in pushes if "current_activity" in p and p["current_activity"]]
    assert activity_pushes, "intermediate pip lines must update current_activity"
    final = pushes[-1]
    assert final["state"] == "restart_pending"
    assert final["current_activity"] == ""
    assert "Successfully installed" in final["pip_output"]


def test_pip_executor_failure_keeps_last_activity_line(monkeypatch):
    pushes = _run_pip_executor(
        monkeypatch,
        ["Collecting workswarm", "ERROR: no matching distribution"],
        returncode=1,
    )
    assert pushes[0]["current_activity"] == ""
    # The pip output is throttled (one line per 0.5s), so only the first
    # line is guaranteed to become an activity push. The failure-path
    # callback must not clear current_activity: the last pushed activity
    # line stays visible in the panel as a diagnostic clue.
    pushed_activities = [p["current_activity"] for p in pushes if p.get("current_activity")]
    assert pushed_activities and pushed_activities[0] == "Collecting workswarm"
    final = pushes[-1]
    assert final["state"] == "update_available"
    assert "pip install failed" in final["error"]
    assert "current_activity" not in final
