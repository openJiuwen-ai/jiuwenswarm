# Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

"""detect_external_cli_agent 携带内置（托管）CLI 路径的行为（#6647）。

配置本地可执行文件后，探测结果与 PATH 扫描都不再指向内置路径，
路径下拉框需要探测响应里的 builtin_path 作为常驻选项，
否则内置路径会从下拉框消失并被历史清理剪掉。
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path

import pytest

from jiuwenswarm.common.config_panel import config_set_handlers as handlers


@pytest.fixture
def builtin_cli(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """内置托管 CLI 的假可执行文件，版本命令与参考版本打桩为一致输出。"""
    managed = tmp_path / "site-packages" / "claude_agent_sdk" / "_bundled" / "claude.exe"
    managed.parent.mkdir(parents=True)
    managed.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(handlers, "external_cli_managed_cli_path", lambda _agent: str(managed))
    monkeypatch.setattr(
        handlers, "run_external_cli_version_command", lambda _agent, _path: ("1.2.3 (Claude Code)", "")
    )
    # 参考版本与桩版本一致，避免环境里真实 SDK 版本把状态推成 warning。
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _agent: "1.2.3")
    return managed


def test_detect_result_carries_builtin_path_for_missing_local_path(
    tmp_path: Path, builtin_cli: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """配置的本地路径不存在时探测失败，内置路径仍随结果返回。

    显式传入的缺失路径不会触发 PATH 回退（resolve 对显式路径直接判
    not_found），因此无需对 shutil.which 打桩即保持确定性。
    """
    missing = tmp_path / "local" / "claude.exe"
    result = handlers.detect_external_cli_agent("claude", str(missing))
    assert result["status"] == "missing"
    assert result["path"] == ""
    assert result["builtin_path"] == str(builtin_cli)


def test_detect_result_carries_builtin_path_for_local_path_hit(
    tmp_path: Path, builtin_cli: Path
) -> None:
    """本地路径可用时探测成功，builtin_path 与探测路径并存。"""
    local = tmp_path / "local" / "claude.exe"
    local.parent.mkdir(parents=True)
    local.write_text("stub", encoding="utf-8")
    result = handlers.detect_external_cli_agent("claude", str(local))
    assert result["status"] == "ok"
    assert result["path"] == str(local)
    assert result["builtin_path"] == str(builtin_cli)


def test_detect_result_builtin_path_empty_when_managed_cli_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """内置 CLI 未安装（托管文件缺失）时 builtin_path 为空串。

    PATH 探测必须打桩：cli_path 为空时 detect 回退 shutil.which 查找系统
    PATH，装有 claude 的机器上会探测成功并破坏 missing 断言（!10307 检视）。
    """
    monkeypatch.setattr(handlers, "external_cli_managed_cli_path", lambda _agent: "")
    monkeypatch.setattr(shutil, "which", lambda _cmd, mode=os.F_OK | os.X_OK, path=None: None)
    result = handlers.detect_external_cli_agent("claude", "")
    assert result["status"] == "missing"
    assert result["path"] == ""
    assert result["builtin_path"] == ""


def test_detect_result_builtin_path_empty_for_unsupported_agent(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """未知 agent 走 unavailable 分支，托管路径为空串。"""
    result = handlers.detect_external_cli_agent("definitely-not-an-agent")
    assert result["status"] == "unavailable"
    assert json.dumps(result)  # 结果可序列化
    assert result["builtin_path"] == ""
