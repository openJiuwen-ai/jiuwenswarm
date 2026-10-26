# coding: utf-8
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""resolve_active_office_claw_tool_id must also match LLM-sanitized tool names.

AbilityManager exposes connector tools whose raw names carry dots (e.g.
``aippt.doc_beautify`` from a 金山文档 connector) in a sanitized, provider-safe
form (``aippt_doc_beautify``). The stale-id recovery path resolves a model-facing
name back to the active registration's tool id, so it must match the sanitized
form against the raw name embedded in the id.

依赖较新版本 openjiuwen（agent-core）的 ``name_sanitize``：旧版本下该模块不存在，
jiuwenswarm 容错降级——不启用回退匹配（上游本不产生清洗名，精确匹配已足够），
相关用例 skip；降级行为用例不依赖新模块，任何版本都执行。
"""

import pytest

import jiuwenswarm.common.mcp_config as mcp_config
from jiuwenswarm.common.mcp_config import (
    bind_active_office_claw_mcp_tools,
    resolve_active_office_claw_tool_id,
)

_HAS_NAME_SANITIZE = mcp_config.sanitize_llm_tool_name is not None
pytestmark = pytest.mark.skipif(
    not _HAS_NAME_SANITIZE,
    reason="requires newer openjiuwen (tool.name_sanitize)",
)


def test_resolves_sanitized_name_of_dotted_raw_tool():
    owned = "office-claw-request-abc123.金山文档.aippt.doc_beautify"
    with bind_active_office_claw_mcp_tools([owned]):
        assert resolve_active_office_claw_tool_id("aippt_doc_beautify") == owned
        # Raw name lookups keep working unchanged.
        assert resolve_active_office_claw_tool_id("aippt.doc_beautify") == owned
        assert resolve_active_office_claw_tool_id("missing_tool") is None


def test_resolves_sanitized_name_for_registry_scoped_ids():
    owned = "mcp-registry-request-abc123.金山文档.dbsheet.create_fields"
    with bind_active_office_claw_mcp_tools([owned]):
        assert resolve_active_office_claw_tool_id("dbsheet_create_fields") == owned


def test_direct_tail_match_still_wins_over_sanitized_scan():
    owned = "office-claw-request-abc123.office-claw.office_claw_preview_scheduled_task"
    with bind_active_office_claw_mcp_tools([owned]):
        assert (
            resolve_active_office_claw_tool_id("office_claw_preview_scheduled_task")
            == owned
        )


def test_fallback_disabled_without_name_sanitize(monkeypatch):
    """旧版 agent-core 降级行为：回退匹配关闭。

    直接把模块属性置 None 模拟旧版本导入失败，验证精确匹配不受影响。
    """
    monkeypatch.setattr(mcp_config, "sanitize_llm_tool_name", None)
    owned = "office-claw-request-abc123.金山文档.aippt.doc_beautify"
    with bind_active_office_claw_mcp_tools([owned]):
        # 精确匹配不受影响
        assert resolve_active_office_claw_tool_id("aippt.doc_beautify") == owned
        # 清洗名回退被禁用（上游本来就不产生清洗名）
        assert resolve_active_office_claw_tool_id("aippt_doc_beautify") is None
