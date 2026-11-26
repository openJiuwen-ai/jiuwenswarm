# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Repeated layout migration must not remove data through its own aliases."""

import subprocess
import sys

import pytest

from jiuwenswarm.common import paths


@pytest.mark.parametrize("merge", [paths._migrate_absorb_file, paths._migrate_absorb])
def test_migration_preserves_file_when_source_is_destination(tmp_path, merge):
    record = tmp_path / "session.json"
    record.write_text('{"desired": true}', encoding="utf-8")

    merge(record, record)

    assert record.read_text(encoding="utf-8") == '{"desired": true}'


def test_migration_preserves_checkpoint_through_directory_alias(tmp_path):
    target = tmp_path / "profile" / "agent"
    record = target / "lifecycle" / "session.json"
    record.parent.mkdir(parents=True)
    record.write_text('{"desired": true}', encoding="utf-8")
    checkpoint = target / "checkpoint.db"
    checkpoint.write_bytes(b"durable checkpoint")
    alias = tmp_path / "agent"
    if sys.platform == "win32":
        subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(alias), str(target)],
            check=True, capture_output=True,
        )
    else:
        alias.symlink_to(target, target_is_directory=True)

    paths._move_if_absent(alias, target)
    paths._move_if_absent(alias, target)

    assert record.read_text(encoding="utf-8") == '{"desired": true}'
    assert checkpoint.read_bytes() == b"durable checkpoint"
    assert alias.resolve() == target.resolve()


def test_migration_still_merges_distinct_directories_without_overwrite(tmp_path):
    old = tmp_path / "old"
    new = tmp_path / "new"
    old.mkdir()
    new.mkdir()
    (old / "same.txt").write_text("duplicate", encoding="utf-8")
    (new / "same.txt").write_text("duplicate", encoding="utf-8")
    (old / "different.txt").write_text("old value", encoding="utf-8")
    (new / "different.txt").write_text("canonical", encoding="utf-8")

    paths._move_if_absent(old, new)

    assert not old.exists()
    assert (new / "same.txt").read_text(encoding="utf-8") == "duplicate"
    assert (new / "different.txt").read_text(encoding="utf-8") == "canonical"
    assert (new / "different.txt.migrated").read_text(encoding="utf-8") == "old value"
