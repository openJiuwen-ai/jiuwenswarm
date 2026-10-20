import json
import sys

import pytest

from jiuwenswarm.common import updater
from jiuwenswarm.common import version_source


class OfflineSource:
    def fetch_latest(self, current_version: str = ""):
        del current_version
        raise RuntimeError("network unavailable")


class JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return False

    def read(self) -> bytes:
        return json.dumps(self._payload).encode("utf-8")


@pytest.fixture(autouse=True)
def pip_install_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)


def test_failed_check_keeps_release_metadata_and_invalidates_actions(monkeypatch):
    monkeypatch.setattr(updater, "get_config_raw", lambda: {"updater": {"enabled": True}})
    monkeypatch.setattr(
        updater.UpdaterService,
        "_create_version_source",
        staticmethod(lambda config: OfflineSource()),
    )
    service = updater.UpdaterService()
    service._update_status(
        latest_version="1.2.3",
        has_update=True,
        release_notes="Release notes",
        published_at="2026-10-01T00:00:00Z",
        source_type="pypi",
        matched_asset="workswarm-1.2.3-py3-none-any.whl",
        download_url="https://example.test/workswarm-1.2.3-py3-none-any.whl",
    )

    status = service.check()

    assert status["state"] == "error"
    assert "Update check failed" in status["error"]
    assert status["latest_version"] == "1.2.3"
    assert status["release_notes"] == "Release notes"
    assert status["published_at"] == "2026-10-01T00:00:00Z"
    assert status["source_type"] == "pypi"
    assert status["has_update"] is False
    assert status["asset_name"] == ""
    assert status["matched_asset"] == ""
    assert status["download_url"] == ""


def test_pypi_publish_time_falls_back_to_version_json_api(monkeypatch):
    requested_urls = []

    def fake_urlopen(request, timeout):
        del timeout
        requested_urls.append(request.full_url)
        if len(requested_urls) == 1:
            raise version_source.URLError("mirror unavailable")
        return JsonResponse({
            "urls": [{"upload_time_iso_8601": "2026-10-01T00:00:00Z"}],
        })

    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)
    source = version_source.PyPIVersionSource(
        package="workswarm",
        mirror="https://mirror.test/pypi",
        timeout_seconds=5,
    )

    assert source._fetch_publish_time("1.2.3") == "2026-10-01T00:00:00Z"
    assert requested_urls == [
        "https://mirror.test/pypi/pypi/workswarm/1.2.3/json",
        "https://pypi.org/pypi/workswarm/1.2.3/json",
    ]
