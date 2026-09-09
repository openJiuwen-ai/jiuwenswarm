# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Feishu _detect_workspace_files: per-session project_dir + tenant workspace fallback."""

from __future__ import annotations

import json
import warnings
from pathlib import Path
from unittest.mock import MagicMock

# lark_oapi → pkg_resources deprecation; pytest.ini treats warnings as errors.
warnings.filterwarnings(
    "ignore",
    message=r"pkg_resources is deprecated as an API.*",
    category=UserWarning,
)

from jiuwenswarm.gateway.channel_manager.base import RobotMessageRouter
from jiuwenswarm.gateway.channel_manager.im_platforms.feishu.feishu_connect import (
    FeishuChannel,
    FeishuConfig,
)
from tests.unit_tests.tenant_workspace_test_helpers import (
    patch_multi_tenant_workspace_dirs,
    tenant_workspace_key,
    tenant_workspace_root,
)


def _make_channel() -> FeishuChannel:
    config = FeishuConfig(
        enabled=True,
        app_id="test_app_id",
        app_secret="test_app_secret",
        enable_file_upload=True,
    )
    return FeishuChannel(config, MagicMock(spec=RobotMessageRouter))


def _tenant_agent_root(tmp_path: Path, service_id: str, agent_id: str) -> Path:
    return tenant_workspace_root(tmp_path, tenant_workspace_key(service_id, agent_id)) / "agent"


def _write_session_project_dir(
    sessions_root: Path, session_id: str, project_dir: Path
) -> None:
    meta_dir = sessions_root / session_id
    meta_dir.mkdir(parents=True)
    (meta_dir / "metadata.json").write_text(
        json.dumps({"session_id": session_id, "project_dir": str(project_dir)}),
        encoding="utf-8",
    )


def test_detect_abs_path_under_tenant_workspace(monkeypatch, tmp_path: Path):
    ch = _make_channel()
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    ws = _tenant_agent_root(tmp_path, "office", "bot") / "jiuwenclaw_workspace"
    ws.mkdir(parents=True)
    target = ws / "report.docx"
    target.write_bytes(b"docx")

    abs_fwd = str(target).replace("\\", "/")
    found = ch._detect_workspace_files(
        f"已生成文件 {abs_fwd} ，请查收。",
        "sess_1",
        service_id="office",
        agent_id="bot",
    )
    assert len(found) == 1
    assert Path(found[0]).resolve() == target.resolve()


def test_detect_quoted_filename_uses_session_project_dir(monkeypatch, tmp_path: Path):
    ch = _make_channel()
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    project = tmp_path / "my_project"
    project.mkdir()
    target = project / "notes.pdf"
    target.write_bytes(b"%PDF")
    sessions = _tenant_agent_root(tmp_path, "default", "office") / "sessions"
    _write_session_project_dir(sessions, "sess_proj", project)

    # Default tenant workspace exists but does NOT contain notes.pdf
    ws = _tenant_agent_root(tmp_path, "default", "office") / "jiuwenclaw_workspace"
    ws.mkdir(parents=True)

    found = ch._detect_workspace_files(
        "请下载 'notes.pdf' 查看。",
        "sess_proj",
        service_id="default",
        agent_id="office",
    )
    assert len(found) == 1
    assert Path(found[0]).resolve() == target.resolve()


def test_detect_falls_back_to_tenant_workspace_when_no_project_dir(
    monkeypatch, tmp_path: Path
):
    ch = _make_channel()
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    ws = _tenant_agent_root(tmp_path, "default", "office") / "jiuwenclaw_workspace"
    ws.mkdir(parents=True)
    target = ws / "notes.pdf"
    target.write_bytes(b"%PDF")

    found = ch._detect_workspace_files(
        "请下载 'notes.pdf' 查看。",
        "sess_plain",
        service_id="default",
        agent_id="office",
    )
    assert len(found) == 1
    assert Path(found[0]).resolve() == target.resolve()


def test_detect_does_not_use_other_tenant_workspace(monkeypatch, tmp_path: Path):
    ch = _make_channel()
    patch_multi_tenant_workspace_dirs(monkeypatch, tmp_path)
    other = _tenant_agent_root(tmp_path, "default", "other") / "jiuwenclaw_workspace"
    other.mkdir(parents=True)
    (other / "secret.docx").write_bytes(b"x")
    mine = _tenant_agent_root(tmp_path, "default", "office") / "jiuwenclaw_workspace"
    mine.mkdir(parents=True)

    found = ch._detect_workspace_files(
        "请下载 'secret.docx' 查看。",
        "sess_1",
        service_id="default",
        agent_id="office",
    )
    assert found == []
