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
    # 版本命令打桩：输出需含 "Claude Code" 身份标识（547ffa1b56 引入的校验）
    monkeypatch.setattr(
        handlers,
        "run_external_cli_version_command",
        lambda _agent, _path: ("Claude Code version 1.2.3", ""),

    )
    # 参考版本与桩版本一致，避免环境里真实 SDK 版本把状态推成 warning。
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _agent: "1.2.3")
    # SDK 门控桩：清单模块视为齐备（环境无关的确定性）。
    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", lambda _agent: [])
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


def test_detect_without_path_requires_sdk_and_managed_cli(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """无显式路径的检测回答"内置 SDK+CLI 是否可用"（!10678 讨论）：
    SDK 判定与集群拉成员同标准——runtime 清单 required_modules 全部可导入
    （codex 为 openai_codex + codex_cli_bin 两个模块）；CLI 判定 = 托管
    可执行文件存在。任一不满足即 missing，PATH 上的第三方/垫片 CLI 不再
    参与判定。"""
    managed = tmp_path / "site-packages" / "codex_cli_bin" / "bin" / "codex.exe"
    managed.parent.mkdir(parents=True)
    managed.write_text("stub", encoding="utf-8")
    monkeypatch.setattr(handlers, "external_cli_managed_cli_path", lambda _agent: str(managed))
    monkeypatch.setattr(
        handlers,
        "run_external_cli_version_command",
        lambda _agent, _path: ("codex-cli 0.171.0", ""),
    )
    real_which = shutil.which
    fake_path_exe = tmp_path / "elsewhere" / "codex.exe"
    fake_path_exe.parent.mkdir(parents=True)
    fake_path_exe.write_text("stub", encoding="utf-8")
    # 层1——清单模块缺失（模拟 openai_codex 未装）：托管文件在也不算可用；
    # PATH 上即便有真 exe 也不参与判定（which 只会被 resolve 用于显式路径，
    # 门控本身不调用）
    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", lambda _agent: ["openai_codex"])
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _agent: "0.171.0")
    monkeypatch.setattr(shutil, "which", lambda _cmd, mode=os.F_OK | os.X_OK, path=None: str(fake_path_exe))
    result = handlers.detect_external_cli_agent("codex", "")
    assert result["status"] == "missing"
    assert result["path"] == ""
    assert result["reason"] == "builtin_not_installed"
    assert "missing modules openai_codex" in result["message"]
    # 层2——清单模块齐备但托管 CLI 缺失：missing（恢复真实 which，
    # 托管校验走文件系统）
    monkeypatch.setattr(shutil, "which", real_which)
    monkeypatch.setattr(handlers, "missing_external_cli_sdk_modules", lambda _agent: [])
    monkeypatch.setattr(handlers, "external_cli_managed_cli_path", lambda _agent: "")
    result = handlers.detect_external_cli_agent("codex", "")
    assert result["status"] == "missing"
    assert "managed cli missing" in result["message"]
    # 层3——新增：reference_version 为空（SDK 发行物在当前进程不可见）→ missing
    monkeypatch.setattr(handlers, "external_cli_managed_cli_path", lambda _agent: str(managed))
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _agent: "")
    result = handlers.detect_external_cli_agent("codex", "")
    assert result["status"] == "missing"
    assert "reference version unavailable" in result["message"]
    # 三层齐备 → 探测托管 CLI → ok
    monkeypatch.setattr(handlers, "external_cli_reference_version", lambda _agent: "0.171.0")
    result = handlers.detect_external_cli_agent("codex", "")
    assert result["status"] == "ok"
    assert result["path"] == str(managed)
    assert result["version"] == "0.171.0"
