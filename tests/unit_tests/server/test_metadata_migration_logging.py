from unittest.mock import Mock

import pytest

from jiuwenswarm.server.runtime.session.lifecycle import LifecycleError
from jiuwenswarm.server.runtime.session import session_metadata as sm


@pytest.fixture
def log(monkeypatch):
    logger = Mock()
    monkeypatch.setattr(sm, 'logger', logger)
    return logger


@pytest.mark.parametrize("error", [
    sm._ObsoleteMetadataMigration("source removed"),
    LifecycleError("NOT_FOUND", "deleted"),
    LifecycleError("SESSION_ARCHIVED", "archived"),
    LifecycleError("OPERATION_IN_PROGRESS", "stale session writer generation"),
])
def test_expected_races_are_debug_only_for_migrations(log, error):
    sm._log_metadata_write_failure("s", sm._MetadataWriteOptions(migration_key=("root", "s")), error)
    assert log.debug.call_count + log.warning.call_count == 1
    log.debug.assert_called_once()
    log.reset_mock()
    sm._log_metadata_write_failure("s", sm._MetadataWriteOptions(), error)
    log.warning.assert_called_once()


@pytest.mark.parametrize("error", [
    PermissionError("denied"), OSError("disk full"),
    FileNotFoundError("unexpected file disappeared"), ValueError("invalid metadata"),
    LifecycleError("BAD_REQUEST", "path escaped"),
    LifecycleError("CONFLICT", "corrupt lifecycle JSON"),
])
def test_real_migration_failures_still_warn(log, error):
    sm._log_metadata_write_failure("s", sm._MetadataWriteOptions(migration_key=("root", "s")), error)
    assert log.debug.call_count + log.warning.call_count == 1
    log.warning.assert_called_once()


def test_delete_between_stat_and_read_does_not_emit_warning(tmp_path, monkeypatch, log):
    from pathlib import Path
    directory = tmp_path / "s"
    directory.mkdir()
    path = directory / "metadata.json"
    path.write_text("{}", encoding="utf-8")
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: tmp_path)
    def removed(_self, **_kwargs):
        raise FileNotFoundError("removed concurrently")
    monkeypatch.setattr(Path, "read_text", removed)
    options = sm._MetadataWriteOptions(expected_fields={}, migration_key=(str(tmp_path), "s"))
    with pytest.raises(sm._ObsoleteMetadataMigration) as caught:
        sm._write_metadata_unfenced("s", {"session_id": "s"}, options)
    sm._log_metadata_write_failure("s", options, caught.value)
    log.warning.assert_not_called()
    log.debug.assert_called_once()


def test_corrupt_existing_metadata_is_not_classified_as_deleted(tmp_path, monkeypatch, log):
    directory = tmp_path / "s"
    directory.mkdir()
    (directory / "metadata.json").write_text("broken JSON", encoding="utf-8")
    monkeypatch.setattr(sm, "get_agent_sessions_dir", lambda: tmp_path)
    options = sm._MetadataWriteOptions(expected_fields={}, migration_key=(str(tmp_path), "s"))
    with pytest.raises(ValueError, match="invalid metadata") as caught:
        sm._write_metadata_unfenced("s", {"session_id": "s"}, options)
    sm._log_metadata_write_failure("s", options, caught.value)
    assert log.warning.called
    log.debug.assert_not_called()
