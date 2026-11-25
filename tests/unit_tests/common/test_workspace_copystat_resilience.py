# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Unit tests for resilient workspace seeding when copystat fails on shared volumes."""

from __future__ import annotations

import errno
import logging
import os
import shutil
from pathlib import Path

import pytest

from jiuwenswarm.common import utils


def _enotsup_errno() -> int:
    return getattr(errno, "ENOTSUP", errno.EPERM)


@pytest.mark.parametrize(
    "err_no",
    [
        errno.EPERM,
        errno.EACCES,
        _enotsup_errno(),
    ],
)
def test_copy_dir_tree_resilient_tolerates_copystat_restrictions(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
    err_no: int,
) -> None:
    src = tmp_path / "src"
    (src / "memory").mkdir(parents=True)
    (src / "keep.txt").write_text("from-template", encoding="utf-8")
    (src / "skip_ZH.md").write_text("ignored", encoding="utf-8")
    (src / "memory" / "new.md").write_text("new-memory", encoding="utf-8")
    (src / "memory" / "existing.md").write_text("template-memory", encoding="utf-8")

    dst = tmp_path / "dst"
    (dst / "memory").mkdir(parents=True)
    (dst / "memory" / "existing.md").write_text("user-custom", encoding="utf-8")

    def fail_copystat(src_path, dst_path, *, follow_symlinks=True):
        if err_no in (errno.EPERM, errno.EACCES):
            raise PermissionError(err_no, os.strerror(err_no), str(dst_path))
        raise OSError(err_no, os.strerror(err_no), str(dst_path))

    monkeypatch.setattr(shutil, "copystat", fail_copystat)

    with caplog.at_level(logging.WARNING):
        utils._copy_dir_tree_resilient(
            src,
            dst,
            ignore_patterns=("*_ZH.md",),
            skip_existing_files=True,
        )

    assert (dst / "keep.txt").read_text(encoding="utf-8") == "from-template"
    assert not (dst / "skip_ZH.md").exists()
    assert (dst / "memory" / "new.md").read_text(encoding="utf-8") == "new-memory"
    assert (dst / "memory" / "existing.md").read_text(encoding="utf-8") == "user-custom"
    assert any("Skipping metadata sync" in record.message for record in caplog.records)


def test_copy_dir_tree_resilient_propagates_real_io_and_write_errors(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = tmp_path / "src"
    src.mkdir()
    (src / "data.txt").write_text("payload", encoding="utf-8")

    # Non-permission copystat error (e.g. EIO) must propagate.
    dst_io = tmp_path / "dst_io"

    def fail_copystat_eio(src_path, dst_path, *, follow_symlinks=True):
        raise OSError(errno.EIO, "Input/output error", str(dst_path))

    monkeypatch.setattr(shutil, "copystat", fail_copystat_eio)
    with pytest.raises(OSError) as exc_io:
        utils._copy_dir_tree_resilient(src, dst_io)
    assert exc_io.value.errno == errno.EIO

    # Actual file write failure (copyfile EACCES) must never be swallowed.
    monkeypatch.undo()
    dst_write = tmp_path / "dst_write"

    def fail_copyfile(src_path, dst_path, *, follow_symlinks=True):
        raise PermissionError(errno.EACCES, "Permission denied", str(dst_path))

    monkeypatch.setattr(shutil, "copyfile", fail_copyfile)
    with pytest.raises(PermissionError) as exc_write:
        utils._copy_dir_tree_resilient(src, dst_write)
    assert exc_write.value.errno == errno.EACCES


def test_prepare_workspace_tolerates_shared_pvc_dir_copystat_eperm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    workspace_dir = tmp_path / ".jiuwenswarm"
    utils.prepare_workspace(
        overwrite=False,
        preferred_language="en",
        workspace_dir=workspace_dir,
        migrate_config=False,
    )

    # Simulate Pod rebuild on shared PVC + emptyDir infra:
    # config.yaml is absent (emptyDir wiped), agent/workspace & memory already
    # exist with another UID owner (chmod/utime return EPERM), one template
    # file is missing while another customized user file must be preserved.
    config_file = workspace_dir / "config" / "config.yaml"
    config_file.unlink()
    agent_workspace = workspace_dir / "agent" / "workspace"
    agent_md = agent_workspace / "AGENT.md"
    if agent_md.exists():
        agent_md.unlink()
    soul_md = agent_workspace / "SOUL.md"
    soul_md.write_text("custom-soul-content", encoding="utf-8")

    real_chmod = os.chmod
    real_utime = os.utime
    target_prefix = str(agent_workspace)

    def eperm_chmod(
        path,
        mode,
        *,
        dir_fd=None,
        follow_symlinks=True,
    ):
        if str(path).startswith(target_prefix):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        if dir_fd is None and follow_symlinks:
            return real_chmod(path, mode)
        return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    def eperm_utime(
        path,
        times=None,
        *,
        ns=None,
        dir_fd=None,
        follow_symlinks=True,
    ):
        if str(path).startswith(target_prefix):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        if dir_fd is None and follow_symlinks:
            if ns is not None:
                return real_utime(path, ns=ns)
            return real_utime(path, times=times)
        if ns is not None:
            return real_utime(path, ns=ns, dir_fd=dir_fd, follow_symlinks=follow_symlinks)
        return real_utime(path, times=times, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chmod", eperm_chmod)
    monkeypatch.setattr(os, "utime", eperm_utime)

    with caplog.at_level(logging.WARNING):
        diff = utils.prepare_workspace(
            overwrite=False,
            preferred_language="en",
            workspace_dir=workspace_dir,
            migrate_config=False,
        )

    assert config_file.is_file()
    assert agent_md.is_file()
    assert soul_md.read_text(encoding="utf-8") == "custom-soul-content"
    assert str(agent_md) in diff.added_files
    assert any("Skipping metadata sync" in record.message for record in caplog.records)


def test_prepare_runtime_workspace_succeeds_under_shared_pvc_copystat_eperm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    data_dir = tmp_path / "shared_root"
    agent_root = data_dir / "agent"
    agent_workspace = agent_root / "workspace"
    memory_dir = agent_workspace / "memory"
    memory_dir.mkdir(parents=True)

    monkeypatch.setattr(utils, "get_user_workspace_dir", lambda: data_dir)
    monkeypatch.setattr(utils, "get_agent_skills_dir", lambda: agent_workspace / "skills")
    for attr, stub in (
        ("migrate_profile_data_root_layout", lambda: None),
        ("get_agent_root_dir", lambda: agent_root),
        ("get_agent_workspace_dir", lambda: agent_workspace),
        ("get_agent_teams_dir", lambda: data_dir / ".agent_teams"),
        ("reset_path_caches", lambda: None),
        ("reset_config_path_cache", lambda: None),
    ):
        if hasattr(utils, attr):
            monkeypatch.setattr(utils, attr, stub)

    real_chmod = os.chmod
    restricted_paths = {
        str(agent_workspace),
        str(memory_dir),
        str(data_dir / "config" / "config.yaml"),
        str(agent_workspace / "AGENT.md"),
    }

    def eperm_chmod(
        path,
        mode,
        *,
        dir_fd=None,
        follow_symlinks=True,
    ):
        if str(path) in restricted_paths:
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        if dir_fd is None and follow_symlinks:
            return real_chmod(path, mode)
        return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chmod", eperm_chmod)

    with caplog.at_level(logging.WARNING):
        utils.prepare_runtime_workspace(
            cleanup_stale_descs=False,
            migrate_config=False,
        )

    assert (data_dir / "config" / "config.yaml").is_file()
    assert (agent_workspace / "AGENT.md").is_file()
    assert memory_dir.is_dir()
    assert any("Skipping metadata sync" in record.message for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)


def test_install_default_builtin_skills_overwrite_tolerates_copystat_eperm(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    builtin_dir = tmp_path / "builtin"
    catalog_src = builtin_dir / "workswarm-builtin"
    (catalog_src / "references").mkdir(parents=True)
    (catalog_src / "SKILL.md").write_text("# builtin v2", encoding="utf-8")
    (catalog_src / "references" / "guide.md").write_text("guide v2", encoding="utf-8")
    (catalog_src / "legacy_hashes.json").write_text("{}", encoding="utf-8")

    user_skills_dir = tmp_path / "user_skills"
    catalog_dst = user_skills_dir / "workswarm-builtin"
    (catalog_dst / "references").mkdir(parents=True)
    (catalog_dst / "SKILL.md").write_text("# builtin v1", encoding="utf-8")

    real_chmod = os.chmod
    target_prefix = str(catalog_dst)

    def eperm_chmod(
        path,
        mode,
        *,
        dir_fd=None,
        follow_symlinks=True,
    ):
        if str(path).startswith(target_prefix):
            raise PermissionError(errno.EPERM, "Operation not permitted", str(path))
        if dir_fd is None and follow_symlinks:
            return real_chmod(path, mode)
        return real_chmod(path, mode, dir_fd=dir_fd, follow_symlinks=follow_symlinks)

    monkeypatch.setattr(os, "chmod", eperm_chmod)

    diff = utils.CopyDiffResult([], [], [])
    with caplog.at_level(logging.WARNING):
        utils._install_default_builtin_skills(
            builtin_dir=builtin_dir,
            user_skills_dir=user_skills_dir,
            overwrite=True,
            cumulative_diff=diff,
        )

    assert (catalog_dst / "SKILL.md").read_text(encoding="utf-8") == "# builtin v2"
    assert (catalog_dst / "references" / "guide.md").read_text(encoding="utf-8") == "guide v2"
    assert any("Skipping metadata sync" in record.message for record in caplog.records)
    assert not any(record.levelno >= logging.ERROR for record in caplog.records)
