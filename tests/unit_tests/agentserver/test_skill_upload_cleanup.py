# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""企业版 Skill 上传暂存：过期包物理清理只按保留期删除。"""

from __future__ import annotations

import json
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.skill import skill_upload_approval as approval
from jiuwenswarm.server.runtime.skill import skill_upload_cleanup as cleanup


class _FakeStore:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.modified: dict[str, datetime] = {}
        self.removed: list[str] = []

    def list_keys(self, prefix: str) -> list[tuple[str, datetime]]:
        return sorted(
            (key, self.modified[key])
            for key in self.objects
            if key.startswith(prefix)
        )

    def remove_key(self, key: str) -> None:
        self.removed.append(key)
        self.objects.pop(key, None)
        self.modified.pop(key, None)


@pytest.fixture(name="pending_root")
def _pending_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    root = tmp_path / "_skill_upload_pending"
    root.mkdir()
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill.skill_upload_approval.stage_root",
        lambda: root,
    )
    return root


@pytest.fixture(name="store")
def _store(monkeypatch: pytest.MonkeyPatch) -> _FakeStore:
    fake = _FakeStore()
    monkeypatch.setattr(
        "jiuwenswarm.common.obs_files.AttachmentStore.from_env",
        classmethod(lambda cls: fake),
    )
    return fake


def _stage(root: Path, upload_id: str, *, expires_at: str, object_key: str = "") -> Path:
    staging = root / upload_id
    staging.mkdir()
    (staging / "manifest.json").write_text(
        json.dumps({"expires_at": expires_at, "object_key": object_key}),
        encoding="utf-8",
    )
    return staging


def _age(staging: Path, *, hours: float) -> None:
    """把目录 mtime 调早，模拟长时间没有写入的残留目录。"""
    stamp = (datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp()
    os.utime(staging, (stamp, stamp))


def test_purge_expired_staging_keeps_unexpired(
    pending_root: Path, store: _FakeStore
) -> None:
    now = datetime.now(timezone.utc)
    fresh = _stage(
        pending_root,
        "a" * 32,
        expires_at=(now + timedelta(days=1)).isoformat(),
    )
    stale_key = "skill-approvals/" + "b" * 32 + "/" + "c" * 32 + "/old.zip"
    stale = _stage(
        pending_root,
        "c" * 32,
        expires_at=(now - timedelta(seconds=1)).isoformat(),
        object_key=stale_key,
    )
    store.objects[stale_key] = b"old"
    orphan = pending_root / ("d" * 32)
    orphan.mkdir()
    # 无清单目录要超过宽限期才删；未过宽限期属于「可能正在暂存」，见下个用例。
    _age(orphan, hours=2)

    removed = cleanup.purge_expired_staging()

    assert removed == 2
    assert fresh.is_dir()
    assert not stale.exists()
    assert not orphan.exists()
    assert stale_key not in store.objects


def test_purge_expired_objects_respects_retention_and_grace(
    store: _FakeStore, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("SKILL_UPLOAD_RETENTION_DAYS", "7")
    now = datetime.now(timezone.utc)
    stale_key = "skill-approvals/" + "a" * 32 + "/" + "b" * 32 + "/old.zip"
    fresh_key = "skill-approvals/" + "a" * 32 + "/" + "c" * 32 + "/new.zip"
    store.objects[stale_key] = b"old"
    store.modified[stale_key] = now - timedelta(days=8)
    store.objects[fresh_key] = b"new"
    store.modified[fresh_key] = now - timedelta(days=6)

    removed = cleanup.purge_expired_objects(now=now)

    assert removed == 1
    assert stale_key not in store.objects
    assert fresh_key in store.objects


def test_purge_expired_staging_keeps_recent_dir_without_manifest(
    pending_root: Path, store: _FakeStore
) -> None:
    """manifest 落盘前目录已存在（解包、算摘要、传 OBS 期间），不得按过期删掉。"""
    in_flight = pending_root / ("a" * 32)
    in_flight.mkdir()
    (in_flight / "content").mkdir()
    (in_flight / "content" / "SKILL.md").write_text("# Body\n", encoding="utf-8")

    removed = cleanup.purge_expired_staging()

    assert removed == 0
    assert in_flight.is_dir()
    assert (in_flight / "content" / "SKILL.md").is_file()
    assert store.removed == []


def test_purge_expired_staging_skips_unrelated_dirs_and_broken_manifest(
    pending_root: Path, store: _FakeStore
) -> None:
    """只清理 upload_id 目录；清单损坏且超过宽限期按过期处理；无 object_key 不触达 OBS。"""
    now = datetime.now(timezone.utc)
    unrelated = pending_root / "not-an-upload-id"
    unrelated.mkdir()
    broken = pending_root / ("e" * 32)
    broken.mkdir()
    (broken / "manifest.json").write_text("{not json", encoding="utf-8")
    _age(broken, hours=2)
    no_key = _stage(
        pending_root,
        "f" * 32,
        expires_at=(now - timedelta(seconds=1)).isoformat(),
    )

    removed = cleanup.purge_expired_staging()

    assert removed == 2
    assert unrelated.is_dir()
    assert not broken.exists()
    assert not no_key.exists()
    assert store.removed == []


@pytest.mark.parametrize(
    "manifest",
    [None, {}, {"expires_at": ""}, {"expires_at": "not-a-date"}],
)
def test_manifest_expired_without_valid_timestamp(manifest: dict | None) -> None:
    """缺少有效 expires_at 的清单一律按过期处理，避免残留包永久留存。"""
    assert approval.manifest_expired(manifest) is True


def test_manifest_expired_reads_naive_timestamp_as_utc() -> None:
    """无时区的时间按 UTC 解释：领先或落后时区的写入都不能误判。"""
    future = (datetime.now() + timedelta(days=1)).isoformat()
    past = (datetime.now() - timedelta(days=2)).isoformat()

    assert approval.manifest_expired({"expires_at": future}) is False
    assert approval.manifest_expired({"expires_at": past}) is True
