import sys

from jiuwenswarm.common import updater


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


def test_gitcode_release_version_can_be_inferred_from_beta_asset_name():
    from jiuwenswarm.common.version_source import GitCodeReleasesSource

    source = GitCodeReleasesSource(owner="openJiuwen", repo="jiuwenswarm")
    release = source._parse_release({
        "tag_name": "0.2.2",
        "name": "JiuwenSwarm",
        "assets": [
            {
                "name": "JiuwenSwarm-setup-0.2.2.exe",
                "url": "https://example.test/JiuwenSwarm-setup-0.2.2.exe",
            },
            {
                "name": "JiuwenSwarm-setup-0.2.3.beta1.exe",
                "url": "https://example.test/JiuwenSwarm-setup-0.2.3.beta1.exe",
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

    release = source._fetch_newest_from_list("https://example.test/releases", {})

    assert release is not None
    assert release.version == "0.2.3.beta1"
    assert release.prerelease is True


def test_desktop_check_matches_beta_windows_installer(monkeypatch):
    from jiuwenswarm.common.version_source import ReleaseAsset, ReleaseInfo

    monkeypatch.delattr(sys, "frozen", raising=False)
    monkeypatch.setenv("JIUWENSWARM_DESKTOP", "1")

    class FakeSource:
        def fetch_latest(self):
            return ReleaseInfo(
                version="0.2.3.beta1",
                assets=[
                    ReleaseAsset(
                        name="JiuwenSwarm-setup-0.2.2.exe",
                        download_url="https://example.test/JiuwenSwarm-setup-0.2.2.exe",
                    ),
                    ReleaseAsset(
                        name="JiuwenSwarm-setup-0.2.3.beta1.exe",
                        download_url="https://example.test/JiuwenSwarm-setup-0.2.3.beta1.exe",
                    ),
                ],
                source_type="gitcode",
            )

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(updater, "__version__", "0.2.2")
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
            }
        },
    )

    status = updater.UpdaterService().check(manual=True)

    assert status["latest_version"] == "0.2.3.beta1"
    assert status["matched_asset"] == "JiuwenSwarm-setup-0.2.3.beta1.exe"
