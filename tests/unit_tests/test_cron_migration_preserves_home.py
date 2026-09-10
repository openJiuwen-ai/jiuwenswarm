"""Workspace migration must preserve cron stores and agent/home in place."""

import json

import pytest

from jiuwenswarm.common import utils


@pytest.fixture
def home(tmp_path):
    path = tmp_path / "agent/home"
    path.mkdir(parents=True)
    (path / "cron_jobs.json").write_text(
        json.dumps({"version": 1, "jobs": [{"id": "old"}]}), encoding="utf-8",
    )
    (path / "heartbeat_jobs.json").write_text("heartbeat", encoding="utf-8")
    (path / "other").mkdir()
    (path / "other/history.json").write_text("history", encoding="utf-8")
    return path


def assert_home_preserved(home):
    assert (home / "heartbeat_jobs.json").read_text(encoding="utf-8") == "heartbeat"
    assert (home / "other/history.json").read_text(encoding="utf-8") == "history"


@pytest.mark.parametrize("existing_gateway", [False, True])
@pytest.mark.parametrize("invalid_json", [False, True])
def test_workspace_migration_leaves_cron_in_place(
    tmp_path, home, existing_gateway, invalid_json, monkeypatch,
):
    source = home / "cron_jobs.json"
    target = tmp_path / "gateway/cron_jobs.json"
    if invalid_json:
        source.write_text("{broken", encoding="utf-8")
    original = source.read_bytes()
    if existing_gateway:
        target.parent.mkdir()
        target.write_text('{"jobs": [{"id": "gateway"}]}', encoding="utf-8")
    gateway_bytes = target.read_bytes() if existing_gateway else None

    utils._migrate_legacy_workspace(tmp_path)

    assert source.read_bytes() == original
    assert_home_preserved(home)
    if existing_gateway:
        assert target.read_bytes() == gateway_bytes
        assert not list(target.parent.glob("cron_jobs.json.backup.*"))
    else:
        assert not target.parent.exists()
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    assert utils.get_cron_jobs_path() == source
    assert utils.get_heartbeat_jobs_path() == home / "heartbeat_jobs.json"


def test_init_does_not_remove_home(tmp_path, home, monkeypatch):
    monkeypatch.setattr(utils, "_find_package_root", lambda: tmp_path)
    monkeypatch.setattr(utils, "_migrate_jiuwenclaw_workspace_to_workspace", lambda _: None)

    class ReachedConfig(Exception):
        pass

    def stop_before_config():
        raise ReachedConfig

    monkeypatch.setattr(utils, "_find_config_template_path", stop_before_config)
    original = (home / "cron_jobs.json").read_bytes()
    with pytest.raises(ReachedConfig):
        utils.prepare_workspace(overwrite=True, workspace_dir=tmp_path)
    assert (home / "cron_jobs.json").read_bytes() == original
    assert_home_preserved(home)
