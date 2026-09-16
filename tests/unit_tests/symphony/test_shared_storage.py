"""Tests for the S3 directory cache root in ``symphony.shared.storage``."""

from __future__ import annotations

from hashlib import sha1
import os
from pathlib import Path
import stat
import tempfile

import pytest

from jiuwenswarm.symphony.shared import storage


def _expected_tag() -> str:
    getuid = getattr(os, "getuid", None)
    return str(getuid()) if getuid is not None else storage._current_user_tag()


def test_user_cache_root_is_scoped_to_the_current_account(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    root = storage.user_cache_root("retriever-index-add-cache")

    assert root.parent == tmp_path
    assert root.name == f"retriever-index-add-cache-{_expected_tag()}"
    assert root.is_dir()


def test_user_cache_root_falls_back_to_the_user_name_without_getuid(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(os, "getuid", raising=False)
    monkeypatch.setattr("getpass.getuser", lambda: "Some User/1")

    root = storage.user_cache_root("s3-dir-cache")

    # The name is sanitised so it stays a single, well-formed path component.
    assert root.parent == tmp_path
    assert root.name == "s3-dir-cache-Some_User_1"


def test_user_cache_root_tolerates_an_unnamed_account(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    monkeypatch.delattr(os, "getuid", raising=False)

    def _no_user():
        raise OSError("no login name")

    monkeypatch.setattr("getpass.getuser", _no_user)

    assert storage.user_cache_root("s3-dir-cache").name == "s3-dir-cache-unknown"


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_user_cache_root_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    root = storage.user_cache_root("s3-dir-cache")

    assert stat.S_IMODE(root.stat().st_mode) == 0o700


def test_user_cache_root_is_idempotent(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    first = storage.user_cache_root("s3-dir-cache")
    (first / "keep").write_text("kept", encoding="utf-8")
    second = storage.user_cache_root("s3-dir-cache")

    assert first == second
    assert (second / "keep").read_text(encoding="utf-8") == "kept"


def test_user_cache_root_reuses_an_existing_private_root(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    existing = tmp_path / f"s3-dir-cache-{_expected_tag()}"
    existing.mkdir(mode=0o700)
    (existing / "cached").write_text("cached", encoding="utf-8")

    root = storage.user_cache_root("s3-dir-cache")

    # Reuse is the normal case, so an own, private root is taken as it stands.
    assert root == existing
    assert (root / "cached").read_text(encoding="utf-8") == "cached"


@pytest.mark.skipif(os.name == "nt", reason="POSIX symlink semantics")
def test_user_cache_root_rejects_a_planted_symlink(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    planted = tmp_path / "planted"
    planted.mkdir()
    (planted / "manifest.json").write_text("planted", encoding="utf-8")
    link = tmp_path / f"s3-dir-cache-{_expected_tag()}"
    link.symlink_to(planted, target_is_directory=True)

    with pytest.raises(RuntimeError, match="not a directory"):
        storage.user_cache_root("s3-dir-cache")

    # The link itself is inspected: it is neither followed nor replaced.
    assert link.is_symlink()
    assert [entry.name for entry in planted.iterdir()] == ["manifest.json"]


@pytest.mark.skipif(os.name == "nt", reason="POSIX permission bits")
def test_user_cache_root_rejects_a_root_reachable_by_other_accounts(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    (tmp_path / f"s3-dir-cache-{_expected_tag()}").mkdir(mode=0o707)

    with pytest.raises(RuntimeError, match="not private"):
        storage.user_cache_root("s3-dir-cache")


@pytest.mark.skipif(not hasattr(os, "getuid"), reason="POSIX ownership")
def test_user_cache_root_rejects_a_root_owned_by_another_account(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))
    # The directory below belongs to the account running the test, so claiming
    # a different identity turns it into a foreign one.
    monkeypatch.setattr(os, "getuid", lambda: os.stat(tmp_path).st_uid + 1)
    (tmp_path / f"s3-dir-cache-{storage._current_user_tag()}").mkdir(mode=0o700)

    with pytest.raises(RuntimeError, match="another account"):
        storage.user_cache_root("s3-dir-cache")


def test_materialize_s3_dir_keeps_the_uri_hash_layout(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    base_uri = "s3://bucket/prefix/index"
    downloaded: list[tuple[str, str, Path]] = []

    def _fake_download(*, base_uri: str, relative_path: str, destination_path: Path) -> bool:
        downloaded.append((base_uri, relative_path, destination_path))
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text("{}", encoding="utf-8")
        return True

    monkeypatch.setattr(storage, "download_s3_relative_object_if_exists", _fake_download)

    local_dir = storage.materialize_s3_dir(base_uri, relative_paths=["manifest.json"])

    expected_key = sha1(base_uri.encode("utf-8")).hexdigest()[:16]
    assert local_dir.name == expected_key
    assert local_dir.parent.name == f"s3-dir-cache-{_expected_tag()}"
    assert local_dir.parent.parent == tmp_path
    assert downloaded == [(base_uri, "manifest.json", local_dir / "manifest.json")]


def test_materialize_s3_dir_separates_distinct_uris(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    def _fake_download(*, base_uri: str, relative_path: str, destination_path: Path) -> bool:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text(base_uri, encoding="utf-8")
        return True

    monkeypatch.setattr(storage, "download_s3_relative_object_if_exists", _fake_download)

    first = storage.materialize_s3_dir("s3://bucket/one", relative_paths=["manifest.json"])
    second = storage.materialize_s3_dir("s3://bucket/two", relative_paths=["manifest.json"])

    assert first != second
    assert first.parent == second.parent


def test_materialize_s3_dir_honours_the_cache_namespace(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "gettempdir", lambda: str(tmp_path))

    def _fake_download(*, base_uri: str, relative_path: str, destination_path: Path) -> bool:
        destination_path.parent.mkdir(parents=True, exist_ok=True)
        destination_path.write_text("{}", encoding="utf-8")
        return True

    monkeypatch.setattr(storage, "download_s3_relative_object_if_exists", _fake_download)

    local_dir = storage.materialize_s3_dir(
        "s3://bucket/prefix",
        relative_paths=["manifest.json"],
        cache_namespace="retriever-s3-index-cache",
    )

    assert local_dir.parent.name == f"retriever-s3-index-cache-{_expected_tag()}"
