# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import os
from pathlib import Path

import pytest

from jiuwenswarm.dotenv_early import load_dotenv_runtime


@pytest.mark.parametrize("dotenv_hosts", ["", "stale.example.com"])
def test_load_dotenv_runtime_preserves_injected_skillhub_hosts(
    tmp_path: Path, monkeypatch, dotenv_hosts: str
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        f"TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS={dotenv_hosts}\n",
        encoding="utf-8",
    )
    monkeypatch.setenv("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS", "downloads.example.com")

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS"] == "downloads.example.com"


def test_load_dotenv_runtime_loads_skillhub_hosts_when_not_injected(
    tmp_path: Path, monkeypatch
):
    env_file = tmp_path / ".env"
    env_file.write_text(
        "TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS=downloads.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.delenv("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS", raising=False)

    load_dotenv_runtime(env_file, override=True)

    assert os.environ["TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS"] == "downloads.example.com"


def test_load_dotenv_runtime_preserves_skillhub_settings_from_template(
    tmp_path: Path, monkeypatch
):
    template = Path(__file__).resolve().parents[2] / "jiuwenswarm/resources/.env.template"
    keys = ("TEAM_SKILLS_HUB_BASE_URL", "TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS")
    lines = [line for line in template.read_text(encoding="utf-8").splitlines()
             if any(line.startswith(f"{key}=") for key in keys)]
    assert len(lines) == len(keys)
    env_file = tmp_path / ".env"
    env_file.write_text("\n".join(lines) + "\nUNRELATED_SETTING=file\n", encoding="utf-8")
    monkeypatch.setenv(keys[0], "https://hub.example.com")
    monkeypatch.setenv(keys[1], "downloads.example.com")
    monkeypatch.setenv("UNRELATED_SETTING", "injected")

    load_dotenv_runtime(env_file, override=True)
    load_dotenv_runtime(env_file, override=True)

    assert os.environ[keys[0]] == "https://hub.example.com"
    assert os.environ[keys[1]] == "downloads.example.com"
    assert os.environ["UNRELATED_SETTING"] == "file"


def test_injected_skillhub_hosts_control_download_validation(tmp_path: Path, monkeypatch):
    from jiuwenswarm.server.runtime.marketplace.hub_package_downloader import (
        HubDownloadError,
        HubPackageDownloader,
    )

    env_file = tmp_path / ".env"
    env_file.write_text("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS=\n", encoding="utf-8")
    monkeypatch.setenv("TEAM_SKILLS_HUB_ALLOWED_DOWNLOAD_HOSTS", "downloads.example.com")

    load_dotenv_runtime(env_file, override=True)
    downloader = HubPackageDownloader()

    downloader.assert_download_url_allowed("https://downloads.example.com/skill.zip")
    with pytest.raises(HubDownloadError) as exc_info:
        downloader.assert_download_url_allowed("https://untrusted.example.com/skill.zip")
    assert exc_info.value.code == "download_blocked"
