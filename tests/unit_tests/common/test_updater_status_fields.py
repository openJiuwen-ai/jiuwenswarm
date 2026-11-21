"""UpdateStatus must serialize the executor's progress fields.

The pip upgrade executor pushes ``current_activity``/``pip_output`` through
``UpdaterService._executor_callback`` -> ``_update_status`` (setattr). They were
missing from the ``UpdateStatus`` dataclass, so ``asdict`` silently dropped
them and the UpdatePanel frontend read perpetual blanks.
"""

from jiuwenswarm.common import updater


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
