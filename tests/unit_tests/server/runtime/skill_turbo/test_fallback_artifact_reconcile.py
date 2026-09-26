# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fallback 契约产物路径归位与路径纪律 prompt 防护。

核心用例：
- 产物路径位于 output_dir 之外时复制归位并回写路径（非流式/流式入口）；
- 跳过条件与 no-op 安全性（无 output_dir / 已在目录内 / 同名不覆盖等）；
- 源根约束：仅归位允许源根（workspace_base，缺失时 output_dir 父目录）
  之内的产物，源根外与 symlink 逃逸一律拒绝（防敏感文件借框架复制外泄）；
- 相对 output_dir 时回写绝对路径，与 subagent 声明形态保持一致；
- validator 拒绝时回滚本次归位产物，不在 output_dir 残留孤儿文件；
- fallback prompt 仅在含 output_dir 时注入路径纪律段。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
    FallbackCall,
    FallbackContractError,
)


class OutlineGenError(RuntimeError):
    """失败节点抛出的业务异常（本地合成）。"""


def _make_handler(contract_output: str) -> DeepAgentFallbackHandler:
    adapter = MagicMock()
    adapter.spawn_fallback = AsyncMock(return_value=contract_output)
    return DeepAgentFallbackHandler(
        adapter, request_id="req-ut", channel_id="officeclaw", session_id="sess-ut"
    )


def _contract_output(outline_path: str) -> str:
    """构造携带产物路径字段的契约声明输出。"""
    return (
        "已生成大纲并写入文件。\n"
        "\n"
        '`{"success": true, "result": {"outline_path": '
        f"{json.dumps(outline_path, ensure_ascii=False)}, "
        '"p4_outline_gen_status": "completed"}}`'
    )


class TestReconcileContractArtifacts:
    def test_relocates_artifact_outside_output_dir(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        outside = tmp_path / "elsewhere" / "outline.md"
        outside.parent.mkdir()
        outside.write_text("# outline", encoding="utf-8")

        contract = {"outline_path": str(outside)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir)}, contract
        )

        relocated = output_dir / "outline.md"
        assert relocated.is_file()
        assert relocated.read_text(encoding="utf-8") == "# outline"
        assert contract["outline_path"] == str(relocated)
        # 复制而非移动，源文件保留
        assert outside.is_file()

    def test_skips_unsafe_or_unnecessary_cases(self, tmp_path, caplog):
        output_dir = tmp_path / "output"
        (output_dir / "nested").mkdir(parents=True)
        existing = output_dir / "outline.md"
        existing.write_text("existing", encoding="utf-8")
        outside = tmp_path / "elsewhere" / "outline.md"
        outside.parent.mkdir()
        outside.write_text("outside", encoding="utf-8")
        inside = output_dir / "nested" / "data.md"
        inside.write_text("inside", encoding="utf-8")

        # output_dir 下已有同名产物：不覆盖，路径保持原值
        # 项目 logging 不向 root 传播，caplog.handler 须手动挂到目标 logger
        target_logger = logging.getLogger(
            "jiuwenswarm.server.runtime.skill_turbo.fallback_handler"
        )
        target_logger.addHandler(caplog.handler)
        caplog.set_level(logging.WARNING, logger=target_logger.name)
        contract = {"outline_path": str(outside)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir)}, contract
        )
        assert existing.read_text(encoding="utf-8") == "existing"
        assert contract["outline_path"] == str(outside)
        # 同名跳过可观测：留下 WARNING 日志
        assert any("relocate skipped, dst exists" in r.message for r in caplog.records)

        # 产物已在 output_dir 内：保持原值
        contract = {"data_path": str(inside)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir)}, contract
        )
        assert contract["data_path"] == str(inside)

        # 非路径 key / 路径不存在：不动作
        contract = {
            "status_text": str(outside),
            "report_path": str(tmp_path / "missing.md"),
        }
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir)}, contract
        )
        assert not (output_dir / "missing.md").exists()

        # 无 output_dir：no-op
        contract = {"outline_path": str(outside)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts({}, contract)
        assert contract["outline_path"] == str(outside)

    def test_rejects_artifact_outside_source_root(self, tmp_path):
        """无 workspace_base 时允许源根为 output_dir 父目录，根外拒绝归位。"""
        output_dir = tmp_path / "output" / "ts1"
        output_dir.mkdir(parents=True)
        secret_dir = tmp_path / "secrets"
        secret_dir.mkdir()
        secret = secret_dir / "id_rsa"
        secret.write_text("PRIVATE KEY", encoding="utf-8")

        contract = {"leak_path": str(secret)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir)}, contract
        )

        # 允许源根（tmp_path/output）之外的敏感路径：不复制、路径保持原值
        assert not (output_dir / "id_rsa").exists()
        assert contract["leak_path"] == str(secret)

    def test_workspace_base_bounds_relocate(self, tmp_path):
        """workspace_base 存在时以其为唯一允许源根（优先于 output_dir 父目录）。"""
        workspace_base = tmp_path / "ws"
        output_dir = tmp_path / "strange" / "place" / "ts1"
        output_dir.mkdir(parents=True)
        # workspace_base 内的幻觉目录（如时间戳拼接错误）：允许归位
        hallucinated = workspace_base / "20260923171654_171727_000"
        hallucinated.mkdir(parents=True)
        artifact = hallucinated / "outline.md"
        artifact.write_text("# outline", encoding="utf-8")
        # output_dir 父目录下但 workspace_base 外：拒绝（证明以 workspace_base 为准）
        near_miss = tmp_path / "strange" / "place" / "near"
        near_miss.mkdir()
        secret = near_miss / "secret.env"
        secret.write_text("TOKEN=1", encoding="utf-8")

        contract = {"outline_path": str(artifact), "leak_file": str(secret)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir), "workspace_base": str(workspace_base)},
            contract,
        )

        assert (output_dir / "outline.md").is_file()
        assert contract["outline_path"] == str(output_dir / "outline.md")
        assert not (output_dir / "secret.env").exists()
        assert contract["leak_file"] == str(secret)

    def test_symlink_escape_rejected(self, tmp_path):
        """允许源根内的符号链接指向根外：resolve 后判定，拒绝归位。"""
        workspace_base = tmp_path / "ws"
        output_dir = workspace_base / "ts1"
        output_dir.mkdir(parents=True)
        secret = tmp_path / "id_rsa"
        secret.write_text("PRIVATE KEY", encoding="utf-8")
        link = workspace_base / "leaked"
        try:
            link.symlink_to(secret)
        except OSError:
            pytest.skip("symlink requires privilege on this platform")

        contract = {"leak_path": str(link)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir), "workspace_base": str(workspace_base)},
            contract,
        )

        assert not (output_dir / "leaked").exists()
        assert contract["leak_path"] == str(link)

    def test_relative_output_dir_relocated_path_is_absolute(
        self, tmp_path, monkeypatch
    ):
        """相对 output_dir 时回写绝对路径，与 subagent 声明形态保持一致。"""
        monkeypatch.chdir(tmp_path)
        workspace_base = tmp_path / "ws"
        output_dir = workspace_base / "ts1"
        output_dir.mkdir(parents=True)
        hallucinated = workspace_base / "20260923171654_171727_000"
        hallucinated.mkdir()
        artifact = hallucinated / "outline.md"
        artifact.write_text("# outline", encoding="utf-8")

        contract = {"outline_path": str(artifact)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": "ws/ts1", "workspace_base": str(workspace_base)}, contract
        )

        relocated = output_dir / "outline.md"
        assert relocated.is_file()
        assert Path(contract["outline_path"]).is_absolute()
        assert Path(contract["outline_path"]) == relocated.resolve()


class TestFallbackReconcileIntegration:
    @pytest.mark.asyncio
    async def test_nonstream_fallback_relocates_artifact(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        outside = tmp_path / "elsewhere" / "outline.md"
        outside.parent.mkdir()
        outside.write_text("# outline", encoding="utf-8")

        handler = _make_handler(_contract_output(str(outside)))
        inputs = {"output_dir": str(output_dir), "topic": "示例主题"}

        result = await handler.fallback(
            FallbackCall(
                node_name="p4_3_outline_gen",
                instruction="## P4.3 大纲生成",
                inputs=inputs,
                error=OutlineGenError("大纲页数校验失败"),
                parent_session=None,
            )
        )

        relocated = output_dir / "outline.md"
        assert relocated.is_file()
        assert result["outline_path"] == str(relocated)
        assert inputs["outline_path"] == str(relocated)

    @pytest.mark.asyncio
    async def test_stream_fallback_relocates_artifact(self, tmp_path):
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        outside = tmp_path / "elsewhere" / "outline.md"
        outside.parent.mkdir()
        outside.write_text("# outline", encoding="utf-8")

        handler = _make_handler(_contract_output(str(outside)))
        inputs = {"output_dir": str(output_dir), "topic": "示例主题"}

        async for _chunk in handler.fallback_stream(
            FallbackCall(
                node_name="p4_3_outline_gen",
                instruction="## P4.3 大纲生成",
                inputs=inputs,
                error=OutlineGenError("大纲页数校验失败"),
                parent_session=None,
            )
        ):
            pass

        assert (output_dir / "outline.md").is_file()
        assert inputs["outline_path"] == str(output_dir / "outline.md")

    @pytest.mark.asyncio
    async def test_validator_rejection_rolls_back_relocated_artifacts(self, tmp_path):
        """归位成功但 validator 拒绝：回滚本次复制，output_dir 不残留孤儿产物。"""
        output_dir = tmp_path / "output"
        output_dir.mkdir()
        outside = tmp_path / "elsewhere" / "outline.md"
        outside.parent.mkdir()
        outside.write_text("# outline", encoding="utf-8")

        handler = _make_handler(_contract_output(str(outside)))
        inputs = {"output_dir": str(output_dir), "topic": "示例主题"}

        def reject_validator(_inputs, _contract_result):
            return "大纲页数不足"

        with pytest.raises(FallbackContractError):
            await handler.fallback(
                FallbackCall(
                    node_name="p4_3_outline_gen",
                    instruction="## P4.3 大纲生成",
                    inputs=inputs,
                    error=OutlineGenError("大纲页数校验失败"),
                    parent_session=None,
                    result_validator=reject_validator,
                )
            )

        # 本次归位复制的文件已回滚，源文件保留
        assert not (output_dir / "outline.md").exists()
        assert outside.is_file()


class TestBuildFallbackQueryPathGuard:
    def test_path_guard_injected_only_when_output_dir_present(self):
        base_inputs = {"topic": "示例主题"}
        query_without = DeepAgentFallbackHandler._build_fallback_query(
            "p4_3_outline_gen",
            "## P4.3 大纲生成",
            base_inputs,
            OutlineGenError("页数不足"),
        )
        # 无 output_dir：不注入纪律段，原有结构保持
        assert "路径纪律" not in query_without
        assert query_without.index("输入参数:") < query_without.index("## 强制输出格式")
        assert "\n\n## 强制输出格式" in query_without

        output_dir = "D:/ws/output/20260923_171727_000"
        query_with = DeepAgentFallbackHandler._build_fallback_query(
            "p4_3_outline_gen",
            "## P4.3 大纲生成",
            {**base_inputs, "output_dir": output_dir},
            OutlineGenError("页数不足"),
        )
        # 有 output_dir：注入纪律段，位于输入参数与输出格式之间
        assert "## 路径纪律（必须遵守）" in query_with
        assert output_dir in query_with
        assert (
            query_with.index("输入参数:")
            < query_with.index("## 路径纪律")
            < query_with.index("## 强制输出格式")
        )
