# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""未发送图片副本只能从当前会话 uploads 目录删除。"""

from __future__ import annotations

from pathlib import Path

import pytest

from jiuwenswarm.server.runtime.attachments.media_attachments import discard_session_upload
from jiuwenswarm.server.runtime.attachments.upload_storage import atomic_write_unique


def _sessions(monkeypatch: pytest.MonkeyPatch, root: Path) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.attachments.media_attachments.get_agent_sessions_dir",
        lambda: root,
    )


def test_discard_removes_file_and_frees_the_name(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(monkeypatch, tmp_path)
    upload_dir = tmp_path / "sess-1" / "uploads"
    upload_dir.mkdir(parents=True)
    target = upload_dir / "sample.png"
    target.write_bytes(b"old")

    assert discard_session_upload("sess-1", str(target)) == {"deleted": True}
    assert not target.exists()

    written = atomic_write_unique(upload_dir / "sample.png", b"new")
    assert written.name == "sample.png"
    assert written.read_bytes() == b"new"


def test_discard_missing_file_is_success(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(monkeypatch, tmp_path)
    missing = tmp_path / "sess-1" / "uploads" / "missing.png"
    missing.parent.mkdir(parents=True)
    assert discard_session_upload("sess-1", str(missing)) == {"deleted": False}


def test_discard_rejects_original_file_outside_uploads(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _sessions(monkeypatch, tmp_path)
    original = tmp_path / "Pictures" / "sample.png"
    original.parent.mkdir()
    original.write_bytes(b"keep")
    (tmp_path / "sess-1" / "uploads").mkdir(parents=True)

    with pytest.raises(ValueError, match="outside session uploads"):
        discard_session_upload("sess-1", str(original))
    assert original.read_bytes() == b"keep"


def test_discard_rejects_other_session_and_parent_traversal(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sessions(monkeypatch, tmp_path)
    other = tmp_path / "other" / "uploads" / "sample.png"
    other.parent.mkdir(parents=True)
    other.write_bytes(b"other")
    secret = tmp_path / "secret.png"
    secret.write_bytes(b"secret")
    (tmp_path / "sess-1" / "uploads").mkdir(parents=True)
    traversal = tmp_path / "sess-1" / "uploads" / ".." / ".." / "secret.png"

    with pytest.raises(ValueError, match="outside session uploads"):
        discard_session_upload("sess-1", str(other))
    with pytest.raises(ValueError, match="outside session uploads"):
        discard_session_upload("sess-1", str(traversal))
    assert other.read_bytes() == b"other"
    assert secret.read_bytes() == b"secret"


def test_discard_rejects_directory_relative_path_and_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    _sessions(monkeypatch, tmp_path)
    upload_dir = tmp_path / "sess-1" / "uploads"
    upload_dir.mkdir(parents=True)
    nested = upload_dir / "nested"
    nested.mkdir()
    outside = tmp_path / "secret.png"
    outside.write_bytes(b"secret")
    link = upload_dir / "link.png"
    link.write_bytes(b"placeholder")

    with pytest.raises(ValueError, match="not a file"):
        discard_session_upload("sess-1", str(nested))
    with pytest.raises(ValueError, match="absolute"):
        discard_session_upload("sess-1", "sample.png")
    assert nested.is_dir()

    real_is_symlink = Path.is_symlink

    def _pretend_link(self: Path) -> bool:
        if self.name == "link.png":
            return True
        return real_is_symlink(self)

    monkeypatch.setattr(Path, "is_symlink", _pretend_link)
    with pytest.raises(ValueError, match="symlink"):
        discard_session_upload("sess-1", str(link))
    assert outside.read_bytes() == b"secret"
    assert link.read_bytes() == b"placeholder"
