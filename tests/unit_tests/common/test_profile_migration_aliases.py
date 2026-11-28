# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Profile migration must never absorb a compatibility alias into itself."""

import os

import pytest

from jiuwenswarm.common import paths


@pytest.mark.skipif(os.name != "nt", reason="Real Windows directory junctions")
@pytest.mark.parametrize("alias_kind", ["agent-child", "workspace"])
def test_profile_migration_preserves_data_reached_through_junction(
    tmp_path, monkeypatch, alias_kind,
):
    import _winapi

    root = tmp_path / "data"
    profile = root / "profile"
    sessions = profile / "agent" / "sessions"
    saved = sessions / "saved-team"
    saved.mkdir(parents=True)
    empty = saved / "empty-directory"
    empty.mkdir()
    metadata = saved / "metadata.json"
    history = saved / "history.jsonl"
    metadata.write_text('{"session_id":"saved-team","team_name":"owner"}', encoding="utf-8")
    history.write_text('{"role":"user","content":"saved input"}\n', encoding="utf-8")
    expected = {item.name: item.read_bytes() for item in (metadata, history)}
    if alias_kind == "agent-child":
        (root / "agent").mkdir()
        _winapi.CreateJunction(str(sessions), str(root / "agent" / "sessions"))
    else:
        _winapi.CreateJunction(str(profile), str(root / "workspace"))

    monkeypatch.setenv("JIUWENSWARM_DATA_DIR", str(root))
    monkeypatch.setattr(paths, "_workspace_base_dir", root)
    monkeypatch.setattr(paths, "_partition_migration_done", False)
    monkeypatch.setattr("jiuwenswarm.enterprise.edition.is_enterprise", lambda: False)
    paths.migrate_profile_data_root_layout()

    assert metadata.read_bytes() == expected[metadata.name]
    assert history.read_bytes() == expected[history.name]
    assert empty.is_dir()


def test_file_absorption_preserves_the_same_entry(tmp_path):
    saved = tmp_path / "metadata.json"
    saved.write_bytes(b"saved metadata")

    paths._migrate_absorb_file(saved, saved)

    assert saved.read_bytes() == b"saved metadata"


def test_file_absorption_still_removes_a_distinct_duplicate(tmp_path):
    old = tmp_path / "old.json"
    new = tmp_path / "new.json"
    old.write_bytes(b"same content")
    new.write_bytes(b"same content")

    paths._migrate_absorb_file(old, new)

    assert not old.exists()
    assert new.read_bytes() == b"same content"
