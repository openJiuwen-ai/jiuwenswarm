import json
import sys
import time

import pytest

from jiuwenswarm.common import updater
from jiuwenswarm.common import version_source


class OfflineSource:
    @staticmethod
    def fetch_latest(current_version: str = ""):
        del current_version
        raise RuntimeError("network unavailable")


class JsonResponse:
    def __init__(self, payload: dict):
        self._payload = payload
        self._closed = False

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        # __exit__ must stay an instance method for the with-protocol to work;
        # touching instance state here also satisfies G.CLS.07.
        self._closed = True
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
    # Seed the last-known release status so the failure branch must preserve it.
    service._update_status(  # pylint: disable=protected-access
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
        # 1st request: simple index (mirror) without upload-time -> empty
        if len(requested_urls) == 1:
            return JsonResponse({
                "files": [{
                    "filename": "workswarm-1.2.3-py3-none-any.whl",
                    "url": "https://mirror.test/pypi/packages/workswarm-1.2.3-py3-none-any.whl",
                }],
            })
        # 2nd request: mirror JSON API unavailable -> fall back upstream
        if len(requested_urls) == 2:
            raise version_source.URLError("mirror unavailable")
        # 3rd request: upstream PyPI JSON API carries the upload time
        return JsonResponse({
            "urls": [{"upload_time_iso_8601": "2026-10-01T00:00:00Z"}],
        })

    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)
    source = version_source.PyPIVersionSource(
        package="workswarm",
        mirror="https://mirror.test/pypi",
        timeout_seconds=5,
    )

    release = source.fetch_latest("1.2.3")

    assert release.published_at == "2026-10-01T00:00:00Z"
    assert requested_urls == [
        "https://mirror.test/pypi/simple/workswarm/",
        "https://mirror.test/pypi/pypi/workswarm/1.2.3/json",
        "https://pypi.org/pypi/workswarm/1.2.3/json",
    ]


def test_publish_time_gives_up_within_budget_when_upstream_hangs(monkeypatch):
    requested_urls = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        # 1st request: simple index (mirror) without upload-time -> empty
        if len(requested_urls) == 1:
            return JsonResponse({
                "files": [{
                    "filename": "workswarm-1.2.3-py3-none-any.whl",
                    "url": "https://mirror.test/pypi/packages/workswarm-1.2.3-py3-none-any.whl",
                }],
            })
        # 2nd request: mirror JSON API reachable but carries no upload time
        if len(requested_urls) == 2:
            return JsonResponse({"releases": {"1.2.3": []}})
        # 3rd request: upstream PyPI hangs. Only the total budget keeps the
        # check from stalling for a full per-request timeout (5s here); the
        # request gets the remaining budget as its socket timeout and then
        # fails, so _fetch_publish_time gives up.
        time.sleep(timeout)
        raise version_source.socket.timeout("simulated hang")

    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)
    monkeypatch.setattr(
        version_source, "PYPI_PUBLISH_TIME_TOTAL_TIMEOUT_SECONDS", 0.05
    )
    source = version_source.PyPIVersionSource(
        package="workswarm",
        mirror="https://mirror.test/pypi",
        timeout_seconds=5,
    )

    start = time.monotonic()
    release = source.fetch_latest("1.2.3")
    elapsed = time.monotonic() - start

    # The version and download info were already resolved from the simple
    # index; the hanging supplementary lookup must not lose them or stall the
    # check past a fraction of the per-request timeout.
    assert requested_urls == [
        "https://mirror.test/pypi/simple/workswarm/",
        "https://mirror.test/pypi/pypi/workswarm/1.2.3/json",
        "https://pypi.org/pypi/workswarm/1.2.3/json",
    ]
    assert release.published_at == ""
    assert release.version == "1.2.3"
    assert release.assets[0].name == "workswarm-1.2.3-py3-none-any.whl"
    assert elapsed < 1.0
