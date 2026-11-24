import json
import sys
import time
from types import SimpleNamespace

import pytest

from jiuwenswarm.common import updater
from jiuwenswarm.common import upgrade_executor
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


@pytest.mark.parametrize("uv_command,index_flag", [(None, "-i"), ("uv", "--index-url")])
@pytest.mark.parametrize(
    "mirror,index_url,project_url",
    [
        (
            "https://mirrors.aliyun.com/pypi",
            "https://mirrors.aliyun.com/pypi/simple/",
            "https://mirrors.aliyun.com/pypi/simple/workswarm/",
        ),
        (
            "https://mirror.test/packages/pypi/simple/",
            "https://mirror.test/packages/pypi/simple/",
            "https://mirror.test/packages/pypi/simple/workswarm/",
        ),
        (
            "https://mirror.test/packages/",
            "https://mirror.test/packages/",
            "https://mirror.test/packages/workswarm/",
        ),
        (
            "https://mirror.test/tenant/+simple?scope=team",
            "https://mirror.test/tenant/+simple?scope=team",
            "https://mirror.test/tenant/+simple/workswarm/?scope=team",
        ),
        (
            "https://mirror.test/pypi",
            "https://mirror.test/pypi",
            "https://mirror.test/pypi/workswarm/",
        ),
        ("", "", "https://pypi.org/simple/workswarm/"),
    ],
)
def test_pypi_check_and_install_share_mirror(
    monkeypatch, uv_command, index_flag, mirror, index_url, project_url
):
    requested_urls = []
    install_commands = []

    def fake_urlopen(request, timeout):
        requested_urls.append(request.full_url)
        return JsonResponse({
            "files": [{
                "filename": "workswarm-1.2.3-py3-none-any.whl",
                "url": "../../packages/workswarm-1.2.3-py3-none-any.whl",
                "upload-time": "2026-10-01T00:00:00Z",
            }],
        })

    def fake_popen(args, **kwargs):
        install_commands.append(args)
        return SimpleNamespace(stdout=None, returncode=0, wait=lambda: 0)

    monkeypatch.setattr(updater, "get_config_raw", lambda: {"updater": {"pypi_mirror": mirror}})
    monkeypatch.setattr(updater, "get_runtime_version", lambda: "1.0.0")
    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)
    monkeypatch.setattr(upgrade_executor.PipExecutor, "_resolve_uv_command", staticmethod(lambda: uv_command))
    monkeypatch.setattr(upgrade_executor.subprocess, "run", lambda *args, **kwargs: SimpleNamespace(returncode=1))
    monkeypatch.setattr(upgrade_executor.subprocess, "Popen", fake_popen)

    service = updater.UpdaterService()
    config = service.get_runtime_config()
    assert service.check()["has_update"] is True
    service.start_download()
    service._download_thread.join(timeout=5)

    assert service.get_status()["state"] == "restart_pending"
    assert requested_urls == [project_url]
    assert config["release_api_url"] == project_url
    assert config["pypi_mirror"] == index_url
    (command,) = install_commands
    assert "workswarm" in command
    if index_url:
        assert command[-2:] == [index_flag, index_url]
    else:
        assert index_flag not in command


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
        mirror="https://mirror.test/pypi/simple/",
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
        mirror="https://mirror.test/pypi/simple/",
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


def test_pypi_source_keeps_only_latest_version_files(monkeypatch):
    """#6849: a simple index lists every upload, not just the newest release."""

    def fake_urlopen(request, timeout):
        del request, timeout
        return JsonResponse({
            "files": [
                {
                    "filename": "workswarm-0.2.5-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.5-py3-none-any.whl",
                    "upload-time": "2026-11-13T10:36:00Z",
                },
                {
                    "filename": "workswarm-0.2.9b3-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9b3-py3-none-any.whl",
                    "upload-time": "2026-11-13T02:36:00Z",
                },
                {
                    "filename": "workswarm-0.2.9-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9-py3-none-any.whl",
                    "upload-time": "2026-11-19T02:06:55Z",
                },
            ],
        })

    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)
    source = version_source.PyPIVersionSource(
        package="workswarm",
        mirror="https://mirror.test/pypi",
        timeout_seconds=5,
    )

    release = source.fetch_latest("0.2.9")

    assert release.version == "0.2.9"
    assert [asset.name for asset in release.assets] == [
        "workswarm-0.2.9-py3-none-any.whl"
    ]
    # 0.2.9 must not borrow 0.2.9b3's timestamp through substring matching.
    assert release.published_at == "2026-11-19T02:06:55Z"


@pytest.mark.parametrize(
    ("installed", "has_update", "matched_asset"),
    [
        ("0.2.9", False, ""),
        ("0.2.8", True, "workswarm-0.2.9b3-py3-none-any.whl"),
        # A dated build suffix marks the artifact, not a newer release, so the
        # installed daily build must not rank below an obsolete pre-release.
        ("0.2.9.dev20261119", False, ""),
        ("0.2.9.beta5.dev20261119", False, ""),
        ("0.2.9.beta1.dev20261119", True, "workswarm-0.2.9b3-py3-none-any.whl"),
    ],
)
def test_pip_check_compares_installed_build_version(
    monkeypatch, installed, has_update, matched_asset
):
    """#6849: the newest mirror release can be older than the installed build."""

    def fake_urlopen(request, timeout):
        del request, timeout
        return JsonResponse({
            "files": [
                {
                    "filename": "workswarm-0.2.5-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.5-py3-none-any.whl",
                    "upload-time": "2026-11-13T10:36:00Z",
                },
                {
                    "filename": "workswarm-0.2.9b3-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9b3-py3-none-any.whl",
                    "upload-time": "2026-11-13T02:36:00Z",
                },
            ],
        })

    monkeypatch.setattr(updater, "get_config_raw", lambda: {"updater": {"enabled": True}})
    monkeypatch.setattr(updater, "get_runtime_version", lambda: installed)
    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)

    status = updater.UpdaterService().check(manual=True)

    assert status["current_version"] == installed
    assert status["latest_version"] == "0.2.9b3"
    assert status["has_update"] is has_update
    assert status["matched_asset"] == matched_asset


@pytest.mark.parametrize(
    ("installed", "expected_latest", "has_update"),
    [
        # The next day's build is an update even though both sides reduce to
        # the same release version once the dated suffix is stripped.
        ("0.2.9.beta5.dev20261119", "0.2.9.beta5.dev20261120", True),
        # The same build is not an update; the source must pick the later
        # artifact instead of the first equal-keyed index entry.
        ("0.2.9.beta5.dev20261120", "0.2.9.beta5.dev20261120", False),
        # An older build must never be offered as an upgrade.
        ("0.2.9.beta5.dev20261121", "0.2.9.beta5.dev20261120", False),
        # A build cut from a newer base is still an upgrade.
        ("0.2.9.beta4.dev20261119", "0.2.9.beta5.dev20261120", True),
        # 0.2.9 outranks a beta5 build of the same base.
        ("0.2.9.dev20261119", "0.2.9.beta5.dev20261120", False),
    ],
)
def test_pip_check_orders_daily_builds_by_build_date(
    monkeypatch, installed, expected_latest, has_update
):
    """#6849 review: a daily channel must not fall back to "always up to date"."""

    def fake_urlopen(request, timeout):
        del request, timeout
        return JsonResponse({
            "files": [
                {
                    "filename": "workswarm-0.2.9b3-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9b3-py3-none-any.whl",
                    "upload-time": "2026-11-13T02:36:00Z",
                },
                {
                    "filename": "workswarm-0.2.9.beta5.dev20261119-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9.beta5.dev20261119-py3-none-any.whl",
                    "upload-time": "2026-11-19T02:36:00Z",
                },
                {
                    "filename": "workswarm-0.2.9.beta5.dev20261120-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9.beta5.dev20261120-py3-none-any.whl",
                    "upload-time": "2026-11-20T02:36:00Z",
                },
            ],
        })

    monkeypatch.setattr(updater, "get_config_raw", lambda: {"updater": {"enabled": True}})
    monkeypatch.setattr(updater, "get_runtime_version", lambda: installed)
    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)

    status = updater.UpdaterService().check(manual=True)

    assert status["current_version"] == installed
    assert status["latest_version"] == expected_latest
    assert status["has_update"] is has_update
    expected_asset = (
        f"workswarm-{expected_latest}-py3-none-any.whl" if has_update else ""
    )
    assert status["matched_asset"] == expected_asset


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.2.9.beta5.dev20261119", "0.2.9.beta5"),
        ("0.2.9.dev20261119", "0.2.9"),
        ("0.2.5.beta1.dev20261009+local", "0.2.5.beta1"),
        ("0.2.9.post20261119", "0.2.9"),
        ("0.2.9+local.20261119", "0.2.9"),
        ("0.2.5.beta1+local.20261009", "0.2.5.beta1"),
        # Release and pre-release versions keep their meaning.
        ("0.2.9b3", "0.2.9b3"),
        ("0.2.9.beta5", "0.2.9.beta5"),
        ("0.2.9.dev1", "0.2.9.dev1"),
        ("", ""),
    ],
)
def test_strip_build_suffix_keeps_release_version(raw, expected):
    """#6849 review: only build-time suffixes are dropped before ordering."""
    assert version_source.strip_build_suffix(raw) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("0.2.9.beta5.dev20261119", 20261119),
        ("0.2.9.post20261119", 20261119),
        ("0.2.9+local.20261119", 20261119),
        ("0.2.9.local.20261119", 20261119),
        ("0.2.5.beta1+local.20261009", 20261009),
        # No 8-digit build date: the segment is not a dated daily build.
        ("0.2.9+local", 0),
        ("0.2.9+local.abc", 0),
        ("0.2.9.dev1", 0),
        ("0.2.9", 0),
    ],
)
def test_build_date_suffix_reads_dev_post_and_local_segments(raw, expected):
    """#6849 review: the +local daily channel must expose its build date."""
    assert version_source.build_date_suffix(raw) == expected


@pytest.mark.parametrize(
    ("installed", "expected_latest", "has_update"),
    [
        # The next day's +local build is an upgrade of the same release.
        ("0.2.9+local.20261119", "0.2.9+local.20261120", True),
        # The same build is not an upgrade; an older one must never be offered.
        ("0.2.9+local.20261120", "0.2.9+local.20261120", False),
        ("0.2.9+local.20261121", "0.2.9+local.20261120", False),
        # A dated build and its clean release are equivalent.
        ("0.2.9", "0.2.9+local.20261120", False),
        # A build cut from a newer release is still an upgrade.
        ("0.2.8", "0.2.9+local.20261120", True),
    ],
)
def test_pip_check_orders_local_daily_builds_by_build_date(
    monkeypatch, installed, expected_latest, has_update
):
    """#6849 review: the +local daily channel must not look always up to date."""

    def fake_urlopen(request, timeout):
        del request, timeout
        return JsonResponse({
            "files": [
                {
                    "filename": "workswarm-0.2.9+local.20261119-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9+local.20261119-py3-none-any.whl",
                    "upload-time": "2026-11-19T02:36:00Z",
                },
                {
                    "filename": "workswarm-0.2.9+local.20261120-py3-none-any.whl",
                    "url": "../../p/workswarm-0.2.9+local.20261120-py3-none-any.whl",
                    "upload-time": "2026-11-20T02:36:00Z",
                },
            ],
        })

    monkeypatch.setattr(updater, "get_config_raw", lambda: {"updater": {"enabled": True}})
    monkeypatch.setattr(updater, "get_runtime_version", lambda: installed)
    monkeypatch.setattr(version_source, "urlopen", fake_urlopen)

    status = updater.UpdaterService().check(manual=True)

    assert status["current_version"] == installed
    assert status["latest_version"] == expected_latest
    assert status["has_update"] is has_update
    expected_asset = (
        f"workswarm-{expected_latest}-py3-none-any.whl" if has_update else ""
    )
    assert status["matched_asset"] == expected_asset


