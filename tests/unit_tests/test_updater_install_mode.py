import sys

import pytest

from jiuwenswarm.common import updater
from jiuwenswarm.common.upgrade_executor import PipExecutor


def test_desktop_env_forces_desktop_install_mode(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")

    assert updater._detect_install_mode() == "desktop"


def test_desktop_env_keeps_gitcode_release_source(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {
            "updater": {
                "enabled": True,
                "desktop_release_api_type": "gitcode",
                "repo_owner": "openJiuwen",
                "repo_name": "jiuwenswarm",
                "release_api_url": "",
                "pypi_mirror": "https://mirrors.aliyun.com/pypi",
            }
        },
    )

    config = updater.UpdaterService._load_config()

    assert config["install_mode"] == "desktop"
    assert config["release_api_type"] == "gitcode"
    assert config["release_api_url"] == (
        "https://api.gitcode.com/api/v5/repos/openJiuwen/jiuwenswarm/releases/latest"
    )


def test_pip_mode_uses_canonical_package_identity(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {
            "updater": {
                "repo_name": "jiuwenswarm",
                "pypi_mirror": "https://mirrors.aliyun.com/pypi",
            }
        },
    )

    config = updater.UpdaterService._load_config()
    source = updater.UpdaterService._create_version_source(config)
    executor = PipExecutor(config, lambda updates: None)
    monkeypatch.setattr(executor, "_resolve_uv_command", lambda: None)

    assert config["package_name"] == "workswarm"
    assert config["repo_name"] == "jiuwenswarm"
    assert config["release_api_url"].endswith("/simple/workswarm/")
    assert source._name == "workswarm"
    assert "workswarm" in executor._build_install_args(
        config["package_name"], config["timeout_seconds"]
    )


def test_pip_executor_installs_canonical_package_name(monkeypatch):
    checked_packages = []
    statuses = []
    executor = PipExecutor(
        {
            "package_name": "workswarm",
            "repo_name": "jiuwenswarm",
            "timeout_seconds": 20,
        },
        statuses.append,
    )

    def reject_editable(package):
        checked_packages.append(package)
        return "editable test stop"

    monkeypatch.setattr(executor, "_check_editable_install", reject_editable)

    executor.install()

    assert checked_packages == ["workswarm"]
    assert statuses[-1]["error"] == "pip install failed: editable test stop"


def test_global_asset_name_pattern_applies_to_all_platforms(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {
            "updater": {
                "asset_name_pattern": "MyApp-{version}.pkg",
            }
        },
    )

    config = updater.UpdaterService._load_config()

    assert config["asset_name_pattern_windows"] == "MyApp-{version}.pkg"
    assert config["asset_name_pattern_macos"] == "MyApp-{version}.pkg"
    assert config["asset_name_pattern_linux"] == "MyApp-{version}.pkg"


def test_platform_asset_name_pattern_overrides_global_pattern(monkeypatch):
    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {
            "updater": {
                "asset_name_pattern": "MyApp-{version}.pkg",
                "asset_name_pattern_windows": "MyAppSetup-{version}.exe",
                "asset_name_pattern_macos": "MyApp-{version}.dmg",
                "asset_name_pattern_linux": "MyApp-{version}.tar.gz",
            }
        },
    )

    config = updater.UpdaterService._load_config()

    assert config["asset_name_pattern_windows"] == "MyAppSetup-{version}.exe"
    assert config["asset_name_pattern_macos"] == "MyApp-{version}.dmg"
    assert config["asset_name_pattern_linux"] == "MyApp-{version}.tar.gz"


def test_release_tag_is_authoritative_over_arbitrary_asset_names():
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")
    release = source._parse_release({
        "tag_name": "release_0.2.4.beta3",
        "name": "Legacy product release",
        "assets": [
            {
                "name": "FutureProduct-9.9.9.exe",
                "url": "https://example.test/FutureProduct-9.9.9.exe",
            },
        ],
    })

    assert release is not None
    assert release.version == "0.2.4.beta3"


def test_release_version_falls_back_to_asset_when_metadata_has_no_version():
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")
    release = source._parse_release({
        "tag_name": "",
        "name": "JiuwenSwarm",
        "assets": [
            {
                "name": "LegacyPackage-0.2.3.beta1.exe",
                "url": "https://example.test/legacy.exe",
            },
        ],
    })

    assert release is not None
    assert release.version == "0.2.3.beta1"


def test_gitcode_release_list_value_wrapper_includes_prerelease(monkeypatch):
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")

    monkeypatch.setattr(
        source,
        "_fetch_json",
        lambda url, headers: {
            "Count": 2,
            "value": [
                {
                    "tag_name": "JiuwenSwarm0.2.2",
                    "created_at": "2026-06-01T08:00:00+08:00",
                    "release_status": "latest",
                    "assets": [
                        {
                            "name": "JiuwenSwarm-setup-0.2.2.exe",
                            "url": "https://example.test/JiuwenSwarm-setup-0.2.2.exe",
                        },
                    ],
                },
                {
                    "tag_name": "0.2.3.beta1",
                    "created_at": "2026-06-02T08:00:00+08:00",
                    "prerelease": True,
                    "release_status": "pre",
                    "assets": [
                        {
                            "name": "JiuwenSwarm-setup-0.2.3.beta1.exe",
                            "url": "https://example.test/JiuwenSwarm-setup-0.2.3.beta1.exe",
                        },
                    ],
                },
            ],
        },
    )

    release = source._fetch_newest_from_list(
        "https://example.test/releases",
        {},
        "0.2.2",
    )

    assert release is not None
    assert release.version == "0.2.3.beta1"
    assert release.prerelease is True
    assert release.current_release_published_at == "2026-06-01T08:00:00+08:00"


def test_release_list_selects_latest_timestamp_not_highest_version(monkeypatch):
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")
    monkeypatch.setattr(
        source,
        "_fetch_json",
        lambda url, headers: [
            {
                "tag_name": "9.0.0",
                "created_at": "2026-06-01T08:00:00+08:00",
            },
            {
                "tag_name": "WorkSwarm-1.0.0",
                "created_at": "2026-06-02T00:30:00Z",
            },
        ],
    )

    release = source._fetch_newest_from_list(
        "https://example.test/releases",
        {},
        "9.0.0",
    )

    assert release is not None
    assert release.version == "1.0.0"
    assert release.current_release_published_at == "2026-06-01T08:00:00+08:00"


def test_release_list_paginates_to_find_installed_release(monkeypatch):
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")

    def fake_fetch(url, headers):
        if url.endswith("page=1"):
            return [
                {
                    "tag_name": f"release_1.0.{index}",
                    "created_at": "2026-06-02T00:00:00Z",
                }
                for index in range(100)
            ]
        assert url.endswith("page=2")
        return [
            {
                "tag_name": "release_0.2.4.beta3",
                "created_at": "2026-06-01T08:00:00+08:00",
            }
        ]

    monkeypatch.setattr(source, "_fetch_json", fake_fetch)

    release = source.fetch_latest("0.2.4.beta3")

    assert release.version == "1.0.0"
    assert release.current_release_published_at == "2026-06-01T08:00:00+08:00"


def test_release_list_keeps_installed_timestamp_when_next_page_fails(monkeypatch):
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")

    def fake_fetch(url, headers):
        if url.endswith("page=1"):
            return [
                {
                    "tag_name": (
                        "release_0.2.4.beta3"
                        if index == 1
                        else f"release_1.0.{index}"
                    ),
                    "created_at": (
                        "2026-06-01T08:00:00+08:00"
                        if index == 1
                        else "2026-06-02T00:00:00Z"
                    ),
                }
                for index in range(100)
            ]
        raise RuntimeError("next page unavailable")

    monkeypatch.setattr(source, "_fetch_json", fake_fetch)

    release = source.fetch_latest("0.2.4.beta3")

    assert release.version == "1.0.0"
    assert release.current_release_published_at == "2026-06-01T08:00:00+08:00"


@pytest.mark.parametrize(
    ("source_type", "latest_asset_key"),
    [("gitcode", "url"), ("github", "browser_download_url")],
)
def test_latest_fallback_fetches_installed_timestamp_by_tag(
    monkeypatch,
    source_type,
    latest_asset_key,
):
    from jiuwenswarm.common.version_source import (
        GitHubReleasesSource,
        GitCodeReleasesSource,
    )

    source = (
        GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")
        if source_type == "gitcode"
        else GitHubReleasesSource(owner="openJiuwen", repo="jiuwenswarm")
    )
    calls = []

    def fake_fetch(url, headers):
        calls.append(url)
        if "?" in url:
            raise RuntimeError("list unavailable")
        if url.endswith("/latest"):
            return {
                "tag_name": "release_0.2.5.beta1",
                "created_at": "2026-06-02T08:00:00+08:00",
                "assets": [
                    {
                        "name": "WorkSwarm-0.2.5.beta1.dmg",
                        latest_asset_key: "https://example.test/WorkSwarm.dmg",
                    }
                ],
            }
        if url.endswith("/tags/release_0.2.4.beta3"):
            return {
                "tag_name": "release_0.2.4.beta3",
                "created_at": "2026-06-01T08:00:00+08:00",
            }
        raise RuntimeError("tag not found")

    monkeypatch.setattr(source, "_fetch_json", fake_fetch)

    release = source.fetch_latest("0.2.4.beta3")

    assert release.version == "0.2.5.beta1"
    assert release.current_release_published_at == "2026-06-01T08:00:00+08:00"
    assert calls[-1].endswith("/tags/release_0.2.4.beta3")


def test_release_timestamp_requires_timezone():
    from jiuwenswarm.common.version_source import release_timestamp_key

    with pytest.raises(ValueError, match="must include a timezone"):
        release_timestamp_key("2026-06-02T08:00:00")


@pytest.mark.parametrize(
    ("platform", "asset_name"),
    [
        ("win32", "TomorrowDesk-preview-installer.exe"),
        ("darwin", "AnotherProduct-nightly.dmg"),
    ],
)
def test_desktop_check_uses_timestamp_and_product_agnostic_installer(
    monkeypatch,
    platform,
    asset_name,
):
    from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")

    class FakeSource:
        def fetch_latest(self, current_version=""):
            assert current_version == "9.0.0"
            return ReleaseInfo(
                version="0.2.3.beta1",
                published_at="2026-06-02T08:00:00+08:00",
                current_release_published_at="2026-06-01T08:00:00+08:00",
                assets=[
                    ReleaseAsset(
                        name="release-notes.txt",
                        download_url="https://example.test/release-notes.txt",
                    ),
                    ReleaseAsset(
                        name=f"../{asset_name}",
                        download_url="https://example.test/unsafe",
                    ),
                    ReleaseAsset(
                        name=asset_name,
                        download_url=f"https://example.test/{asset_name}",
                    ),
                ],
                source_type="gitcode",
            )

    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(updater, "__version__", "9.0.0")
    monkeypatch.setattr(
        updater.UpdaterService,
        "_create_version_source",
        staticmethod(lambda config: FakeSource()),
    )
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {
            "updater": {
                "enabled": True,
                "desktop_release_api_type": "gitcode",
                "asset_name_pattern_windows": "OldProduct-{version}.exe",
                "asset_name_pattern_macos": "OldProduct-{version}.dmg",
            }
        },
    )

    status = updater.UpdaterService().check(manual=True)

    assert status["latest_version"] == "0.2.3.beta1"
    assert status["matched_asset"] == asset_name
    assert status["download_url"] == f"https://example.test/{asset_name}"


@pytest.mark.parametrize(
    ("platform", "legacy_name", "preferred_name"),
    [
        ("win32", "JiuwenSwarm-legacy.exe", "WorkSwarm-current.exe"),
        ("darwin", "JiuwenSwarm-legacy.dmg", "WORKSWARM-current.dmg"),
    ],
)
def test_desktop_check_prefers_workswarm_installer_for_multiple_candidates(
    monkeypatch,
    platform,
    legacy_name,
    preferred_name,
):
    from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

    monkeypatch.setattr(sys, "platform", platform)
    release = ReleaseInfo(
        version="0.2.5.beta1",
        assets=[
            ReleaseAsset(
                name=legacy_name,
                download_url="https://example.test/legacy",
            ),
            ReleaseAsset(
                name=preferred_name,
                download_url="https://example.test/workswarm",
            ),
        ],
    )

    service = updater.UpdaterService()
    service._resolve_desktop_asset({}, release)

    status = service.get_status()
    assert status["matched_asset"] == preferred_name
    assert status["download_url"] == "https://example.test/workswarm"


def test_desktop_check_rejects_ambiguous_same_platform_installers_without_workswarm(
    monkeypatch,
):
    from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

    monkeypatch.setattr(sys, "platform", "darwin")
    release = ReleaseInfo(
        version="0.2.5.beta1",
        assets=[
            ReleaseAsset(
                name="FirstName.dmg",
                download_url="https://example.test/first",
            ),
            ReleaseAsset(
                name="SecondName.dmg",
                download_url="https://example.test/second",
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="Multiple macos desktop installers"):
        updater.UpdaterService()._resolve_desktop_asset({}, release)


def test_desktop_check_rejects_multiple_workswarm_installers(monkeypatch):
    from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

    monkeypatch.setattr(sys, "platform", "win32")
    release = ReleaseInfo(
        version="0.2.5.beta1",
        assets=[
            ReleaseAsset(
                name="WorkSwarm-user.exe",
                download_url="https://example.test/user",
            ),
            ReleaseAsset(
                name="WorkSwarm-machine.exe",
                download_url="https://example.test/machine",
            ),
        ],
    )

    with pytest.raises(RuntimeError, match="Multiple windows desktop installers"):
        updater.UpdaterService()._resolve_desktop_asset({}, release)


@pytest.mark.parametrize(
    "latest_published_at",
    [
        "2026-06-01T00:00:00Z",
        "2026-05-31T23:59:59Z",
    ],
)
def test_desktop_check_does_not_update_for_same_or_older_timestamp(
    monkeypatch,
    latest_published_at,
):
    from jiuwenswarm.common.version_source import ReleaseInfo

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")
    monkeypatch.setattr(sys, "platform", "win32")

    class FakeSource:
        def fetch_latest(self, current_version=""):
            return ReleaseInfo(
                version="99.0.0",
                published_at=latest_published_at,
                current_release_published_at="2026-06-01T08:00:00+08:00",
                source_type="gitcode",
            )

    monkeypatch.setattr(updater, "__version__", "0.2.2")
    monkeypatch.setattr(
        updater.UpdaterService,
        "_create_version_source",
        staticmethod(lambda config: FakeSource()),
    )
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {"updater": {"enabled": True}},
    )

    status = updater.UpdaterService().check(manual=True)

    assert status["state"] == "up_to_date"
    assert status["has_update"] is False


def test_pip_check_keeps_version_comparison(monkeypatch):
    from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.delenv("JIUWENSWARM_DESKTOP", raising=False)

    class FakeSource:
        def fetch_latest(self, current_version=""):
            return ReleaseInfo(
                version="0.2.3.beta1",
                published_at="2020-01-01T00:00:00Z",
                assets=[
                    ReleaseAsset(
                        name="jiuwenswarm-0.2.3b1-py3-none-any.whl",
                        download_url="https://example.test/jiuwenswarm.whl",
                    )
                ],
                source_type="pypi",
            )

    monkeypatch.setattr(updater, "__version__", "0.2.2")
    monkeypatch.setattr(
        updater.UpdaterService,
        "_create_version_source",
        staticmethod(lambda config: FakeSource()),
    )
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {"updater": {"enabled": True}},
    )

    status = updater.UpdaterService().check(manual=True)

    assert status["state"] == "update_available"
    assert status["install_mode"] == "pip"


def test_linux_desktop_keeps_version_comparison(monkeypatch):
    from jiuwenswarm.common.version_source import ReleaseInfo

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")

    class FakeSource:
        def fetch_latest(self, current_version=""):
            return ReleaseInfo(
                version="1.0.0",
                published_at="2026-06-02T08:00:00+08:00",
                current_release_published_at="2026-06-01T08:00:00+08:00",
                source_type="gitcode",
            )

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(updater, "__version__", "9.0.0")
    monkeypatch.setattr(
        updater.UpdaterService,
        "_create_version_source",
        staticmethod(lambda config: FakeSource()),
    )
    monkeypatch.setattr(
        updater,
        "get_config_raw",
        lambda: {"updater": {"enabled": True}},
    )

    status = updater.UpdaterService().check(manual=True)

    assert status["state"] == "up_to_date"
    assert status["has_update"] is False
