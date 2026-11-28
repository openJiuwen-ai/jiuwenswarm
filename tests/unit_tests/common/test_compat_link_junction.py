# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""JW-87: profile 迁移兼容链在无软链权限时的降级与告警。"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pytest

from jiuwenswarm.common import paths


def _denied_symlink(self, target, target_is_directory=False):  # noqa: ANN001, ARG001
    raise PermissionError(1314, "A required privilege is not held by the client.")


@pytest.fixture(autouse=True)
def _reset_warnings():
    paths._COMPAT_LINK_WARNED.clear()
    yield
    paths._COMPAT_LINK_WARNED.clear()


@pytest.mark.skipif(os.name != "nt", reason="junction fallback is Windows-only")
def test_symlink_permission_failure_falls_back_to_junction(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_agent = tmp_path / "profile" / "agent"
    profile_agent.mkdir(parents=True)
    (profile_agent / "marker.txt").write_text("ok", encoding="utf-8")
    link = tmp_path / "agent"

    monkeypatch.setattr(Path, "symlink_to", _denied_symlink)
    try:
        paths._ensure_compat_symlink(link, Path("profile") / "agent")

        assert paths._is_migration_link(link)
        assert link.resolve() == profile_agent.resolve()
        assert (link / "marker.txt").read_text(encoding="utf-8") == "ok"
    finally:
        if paths._is_migration_link(link):
            paths._remove_compat_link(link)


@pytest.mark.skipif(os.name != "nt", reason="junction fallback is Windows-only")
def test_junction_link_is_idempotent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile_agent = tmp_path / "profile" / "agent"
    profile_agent.mkdir(parents=True)
    link = tmp_path / "agent"

    monkeypatch.setattr(Path, "symlink_to", _denied_symlink)
    try:
        paths._ensure_compat_symlink(link, Path("profile") / "agent")
        assert paths._is_migration_link(link)

        # 第二次启动：既使建链能力仍然不可用，也不应重建/破坏已有 junction。
        monkeypatch.setattr(paths, "_create_windows_junction", lambda *_a, **_k: False)
        paths._ensure_compat_symlink(link, Path("profile") / "agent")

        assert paths._is_migration_link(link)
        assert link.resolve() == profile_agent.resolve()
    finally:
        if paths._is_migration_link(link):
            paths._remove_compat_link(link)


@pytest.mark.skipif(os.name != "nt", reason="junction fallback is Windows-only")
def test_junction_creation_handles_special_chars_in_data_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # 数据根可能含 '&'/空格；不能进入 cmd 命令行被当作分隔符
    weird_root = tmp_path / "user & data root"
    profile_agent = weird_root / "profile" / "agent"
    profile_agent.mkdir(parents=True)
    (profile_agent / "marker.txt").write_text("ok", encoding="utf-8")
    link = weird_root / "agent"

    monkeypatch.setattr(Path, "symlink_to", _denied_symlink)
    try:
        paths._ensure_compat_symlink(link, Path("profile") / "agent")
        assert paths._is_migration_link(link)
        assert link.resolve() == profile_agent.resolve()
        assert (link / "marker.txt").read_text(encoding="utf-8") == "ok"
    finally:
        if paths._is_migration_link(link):
            paths._remove_compat_link(link)


@pytest.mark.skipif(os.name != "nt", reason="junction fallback is Windows-only")
def test_junction_pointing_elsewhere_is_replaced(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    old_target = tmp_path / "old"
    old_target.mkdir()
    new_target = tmp_path / "profile" / "agent"
    new_target.mkdir(parents=True)
    link = tmp_path / "agent"
    assert paths._create_windows_junction(link, Path("old"))

    monkeypatch.setattr(Path, "symlink_to", _denied_symlink)
    try:
        paths._ensure_compat_symlink(link, Path("profile") / "agent")
        assert link.resolve() == new_target.resolve()
    finally:
        if paths._is_migration_link(link):
            paths._remove_compat_link(link)


def test_remove_compat_link_is_idempotent(tmp_path: Path) -> None:
    # 并发/异常路径下重复删除不应抛 FileNotFoundError
    missing = tmp_path / "agent"
    paths._remove_compat_link(missing)
    paths._remove_compat_link(missing)


def test_real_path_is_never_overwritten_and_warns(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    link = tmp_path / "agent"
    link.mkdir()
    (link / "keep.txt").write_text("keep", encoding="utf-8")

    with caplog.at_level(logging.WARNING, logger=paths.logger.name):
        paths._ensure_compat_symlink(link, Path("profile") / "agent")

    assert not paths._is_migration_link(link)
    assert (link / "keep.txt").read_text(encoding="utf-8") == "keep"
    assert any("real directory/file" in record.getMessage() for record in caplog.records)


def test_link_failure_warns_instead_of_silent_skip(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    target = tmp_path / "profile" / "agent"
    target.mkdir(parents=True)
    link = tmp_path / "agent"

    monkeypatch.setattr(Path, "symlink_to", _denied_symlink)
    monkeypatch.setattr(paths, "_create_windows_junction", lambda *_a, **_k: False)

    with caplog.at_level(logging.WARNING, logger=paths.logger.name):
        paths._ensure_compat_symlink(link, Path("profile") / "agent")

    assert not link.exists()
    messages = [record.getMessage() for record in caplog.records]
    assert any("could not be created" in message for message in messages)
    assert sum("could not be created" in message for message in messages) == 1

    # 同一进程内重复启动（每次迁移都会再调一遍）不重复刷告警。
    with caplog.at_level(logging.WARNING, logger=paths.logger.name):
        paths._ensure_compat_symlink(link, Path("profile") / "agent")
    assert sum(
        "could not be created" in record.getMessage() for record in caplog.records
    ) == 1
