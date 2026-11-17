# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""_build_env_residual_entity 单测（1001 Python 库与权限：两实体拆分的裁剪逻辑）。

锁定：从 identity 残留「运行环境」块一次性裁出【Python 库清单 + 命令执行权限】，丢弃与
env@1000 重复的平台/Shell/命令差异表；锚点全缺时原样返回（兜底不丢内容）。
"""

from __future__ import annotations

from jiuwenswarm.agents.harness.flash.prompt.content.persona import (
    _ENV_RESIDUAL_TITLE_CN,
    _ENV_RESIDUAL_TITLE_EN,
    _build_env_residual_entity,
)

_CN_BLOCK = (
    "## 运行环境\n\n当前运行平台：win32\n\nShell 能力：...\n\n"
    "Python 库（运行时探测，仅列已探测到的）：\n- Word: python-docx\n以上为已探测到的库。\n\n"
    "**重要提示**：严格匹配平台语法。\n\n常见命令差异对照：\n\n| 6 行表 |\n\n"
    "**特别注意**：mkdir...\n\n## 命令执行环境与权限\n\n- 受限沙箱，仅能读写工作区。"
)

_EN_BLOCK = (
    "## Runtime Environment\n\nCurrent platform: win32\n\n"
    "Python libraries (runtime probe; detected only):\n- Word: python-docx\n"
    "The list above was detected in this runtime.\n\n"
    "**Important**: strictly match the platform.\n\nCommon command differences:\n\n| table |\n\n"
    "**WARNING**: mkdir -p...\n\n"
    "## Command Execution Environment and Permissions\n\n- restricted sandbox."
)


class TestEnvResidualEntity:
    def test_cn_keeps_only_unique_parts(self) -> None:
        out = _build_env_residual_entity(_CN_BLOCK, "cn")
        assert out.startswith(_ENV_RESIDUAL_TITLE_CN)
        assert "Python 库（运行时探测" in out
        assert "## 命令执行环境与权限" in out
        # 丢弃与 env 重复的平台/Shell/命令表
        assert "当前运行平台" not in out
        assert "常见命令差异对照" not in out
        assert "| 6 行表 |" not in out
        assert "**重要提示**" not in out

    def test_en_keeps_only_unique_parts(self) -> None:
        out = _build_env_residual_entity(_EN_BLOCK, "en")
        assert out.startswith(_ENV_RESIDUAL_TITLE_EN)
        assert "Python libraries (runtime probe" in out
        assert "## Command Execution Environment and Permissions" in out
        assert "Current platform" not in out
        assert "Common command differences" not in out

    def test_guard_returns_block_when_no_anchors(self) -> None:
        block = "## 运行环境\n只有一段说明，没有库清单也没有沙箱段。"
        assert _build_env_residual_entity(block, "cn") == block

    def test_sandbox_absent_keeps_python_only(self) -> None:
        block = "## 运行环境\n\nPython 库（运行时探测，仅列已探测到的）：\n- X\n\n**重要提示**：..."
        out = _build_env_residual_entity(block, "cn")
        assert out.startswith(_ENV_RESIDUAL_TITLE_CN)
        assert "Python 库（运行时探测" in out
        assert "## 命令执行环境与权限" not in out

    def test_empty_returns_empty(self) -> None:
        assert _build_env_residual_entity("", "cn") == ""