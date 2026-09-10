"""Workspace migration must only delete a successfully relocated cron store."""

import json
from pathlib import Path

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
def test_only_migrated_cron_is_removed(tmp_path, home, existing_gateway, monkeypatch):
    target = tmp_path / "gateway/cron_jobs.json"
    old_bytes = (home / "cron_jobs.json").read_bytes()
    if existing_gateway:
        target.parent.mkdir()
        target.write_text('{"jobs": [{"id": "gateway"}]}', encoding="utf-8")
    utils._migrate_legacy_workspace(tmp_path)
    assert_home_preserved(home)
    assert not (home / "cron_jobs.json").exists()
    jobs = json.loads(target.read_text(encoding="utf-8"))["jobs"]
    if existing_gateway:
        assert jobs == [{"id": "gateway"}]
        backups = list(target.parent.glob("cron_jobs.json.backup.*"))
        assert len(backups) == 1
        assert backups[0].read_bytes() == old_bytes
    else:
        assert jobs == [{"id": "old", "expired": False}]
    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: tmp_path)
    assert utils.get_cron_jobs_path() == target
    assert utils.get_heartbeat_jobs_path() == home / "heartbeat_jobs.json"


@pytest.mark.parametrize("failure", ["invalid_json", "write", "backup"])
def test_migration_failure_keeps_source(tmp_path, home, monkeypatch, failure):
    source = home / "cron_jobs.json"
    target = tmp_path / "gateway/cron_jobs.json"
    if failure == "invalid_json":
        source.write_text("{broken", encoding="utf-8")
    elif failure == "write":
        original = Path.write_text

        def fail_target(path, *args, **kwargs):
            if path == target:
                raise OSError("write denied")
            return original(path, *args, **kwargs)

        monkeypatch.setattr(Path, "write_text", fail_target)
    else:
        target.parent.mkdir()
        target.write_text('{"jobs": []}', encoding="utf-8")

        def fail_backup(*args, **kwargs):
            raise OSError("backup denied")

        monkeypatch.setattr(utils.shutil, "copy2", fail_backup)
    original = source.read_bytes()
    utils._migrate_legacy_workspace(tmp_path)
    assert source.read_bytes() == original
    assert_home_preserved(home)


def test_unlink_failure_does_not_mask_successful_migration(tmp_path, home, monkeypatch):
    source = home / "cron_jobs.json"
    target = tmp_path / "gateway/cron_jobs.json"
    original = Path.unlink

    def fail_source_unlink(path, *args, **kwargs):
        if path == source:
            raise PermissionError("file in use")
        return original(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", fail_source_unlink)
    utils._migrate_legacy_workspace(tmp_path)
    # The migration itself succeeded; only the source removal failed.
    jobs = json.loads(target.read_text(encoding="utf-8"))["jobs"]
    assert jobs == [{"id": "old", "expired": False}]
    assert source.exists()
    assert_home_preserved(home)


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
