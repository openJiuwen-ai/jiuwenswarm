# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""fallback 契约产物路径归位与路径纪律 prompt 防护。

核心用例：
- 产物路径位于 output_dir 之外时复制归位并回写路径（非流式/流式入口）；
- 跳过条件与 no-op 安全性（无 output_dir / 已在目录内 / 同名不覆盖等）；
- fallback prompt 仅在含 output_dir 时注入路径纪律段。
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from jiuwenswarm.server.runtime.skill_turbo.fallback_handler import (
    DeepAgentFallbackHandler,
    FallbackCall,
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

    def test_skips_unsafe_or_unnecessary_cases(self, tmp_path):
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
        contract = {"outline_path": str(outside)}
        DeepAgentFallbackHandler._reconcile_contract_artifacts(
            {"output_dir": str(output_dir)}, contract
        )
        assert existing.read_text(encoding="utf-8") == "existing"
        assert contract["outline_path"] == str(outside)

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
