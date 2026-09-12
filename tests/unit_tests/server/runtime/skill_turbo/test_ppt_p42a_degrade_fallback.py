# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""P4.2a 查询生成解析失败的兜底降级回归测试。

背景（2026-09-11 PPT 生成任务中断事故）：
P4.2a 的 LLM 输出解析失败时直接抛 ContentPlanError 终止节点；而
PlanNode.run 模板方法在子节点级立即触发 fallback（ContentPlanNode 的
_P4_MAX_ATTEMPTS 循环在 fallback 链上拦不到），最重最脆的兜底层
（27k tokens subagent + 权限链）被直接触发并失败，整条 PPT 流水线终止。

修复契约：P4.2a 解析失败时降级为 topic 单查询继续快速调研
（topic 也为空才上抛），并把原始输出（截断）落 WARNING 日志便于排查。
"""

from __future__ import annotations

import logging

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.content_plan import (
    ContentPlanError,
    _run_p42_quick_research,
)

_LOGGER_NAME = "jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.content_plan"


class _ListHandler(logging.Handler):
    """收集 WARNING 及以上日志消息（用于断言降级日志内容）。

    注：jiuwenswarm 顶层 logger 由队列 handler 接管且 propagate=False，
    pytest caplog 捕获不到，故直接在目标 logger 上挂临时 handler 断言。
    """

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.messages.append(record.getMessage())


class _FakePlanNode:
    """最小 PlanNode 替身：覆盖 _run_p42_quick_research 的全部依赖。"""

    def __init__(self, p42a_raw: str, source_material: str = "") -> None:
        self._p42a_raw = p42a_raw
        self._source_material = source_material
        self.web_search_queries: list[str] = []

    def has_tool(self, name: str) -> bool:
        return name in ("web_search", "read_file")

    async def stream_llm(self, prompt: str, *, system_prompt: str | None = None):
        # P4.2a 输出：整段一次吐出即可（_stream_llm_collect_bounded 逐 chunk 收集）
        yield self._p42a_raw

    async def stream_llm_collect(
        self, prompt: str, *, system_prompt: str | None = None
    ) -> str:
        # 相关性评估：直接 sufficient，跳出重搜循环
        return '{"relevance":"sufficient","reason":"ok","retry_queries":[]}'

    async def call_tool(self, name: str, **kwargs):
        if name == "read_file":
            # 模拟读取用户上传文档（doc_raw_path）的内容
            return self._source_material
        if name == "web_search":
            self.web_search_queries.append(str(kwargs.get("query") or ""))
            return "搜索结果：相关摘要文本（可用于大纲）"
        raise AssertionError(f"unexpected tool: {name}")


@pytest.mark.asyncio
async def test_p42a_parse_failure_degrades_to_topic_query() -> None:
    """解析失败（无任何 JSON）时降级为 topic 单查询，流水线继续。"""
    node = _FakePlanNode("抱歉，本助手暂时无法生成搜索建议。")
    inputs = {"topic": "2026年全球金融科技"}

    await _run_p42_quick_research(node, inputs)

    assert node.web_search_queries == ["2026年全球金融科技"]
    assert inputs["p4_search_queries"] == ["2026年全球金融科技"]
    assert inputs["p4_quick_research_status"] == "completed"
    assert inputs["search_results"][0]["query"] == "2026年全球金融科技"


@pytest.mark.asyncio
async def test_p42a_parse_failure_logs_raw_excerpt() -> None:
    """解析失败时必须把原始输出（截断）落 WARNING 日志，便于定位解析器问题。"""
    handler = _ListHandler()
    target_logger = logging.getLogger(_LOGGER_NAME)
    target_logger.addHandler(handler)
    try:
        node = _FakePlanNode("解析失败的原始输出内容ABC12345")
        await _run_p42_quick_research(node, {"topic": "测试主题"})
    finally:
        target_logger.removeHandler(handler)

    joined = "\n".join(handler.messages)
    assert "ABC12345" in joined


@pytest.mark.asyncio
async def test_p42a_parse_failure_without_topic_still_raises() -> None:
    """兜底守卫：topic 也为空时无法构造降级查询，必须上抛原始异常。"""
    node = _FakePlanNode("无 JSON 乱码输出")
    inputs = {"topic": "   "}

    with pytest.raises(ContentPlanError):
        await _run_p42_quick_research(node, inputs)


@pytest.mark.asyncio
async def test_p42a_valid_output_still_uses_parsed_queries() -> None:
    """正常路径（不得回归）：合法 JSON 输出走解析出的多条查询。"""
    valid = (
        '```json\n{"entity":"金融科技","queries":['
        '{"dimension":"监管","query":"2026 金融科技 监管政策"},'
        '{"dimension":"市场","query":"2026 金融科技 市场规模"},'
        '{"dimension":"技术","query":"2026 金融科技 技术趋势"},'
        '{"dimension":"玩家","query":"2026 金融科技 代表企业"},'
        '{"dimension":"风险","query":"2026 金融科技 风险挑战"}'
        "]}\n```"
    )
    node = _FakePlanNode(valid)
    inputs = {"topic": "2026年全球金融科技"}

    await _run_p42_quick_research(node, inputs)

    assert node.web_search_queries == [
        "2026 金融科技 监管政策",
        "2026 金融科技 市场规模",
        "2026 金融科技 技术趋势",
        "2026 金融科技 代表企业",
        "2026 金融科技 风险挑战",
    ]
    assert inputs["p4_search_queries"] == node.web_search_queries


@pytest.mark.asyncio
async def test_p42a_parse_failure_with_source_material_still_raises() -> None:
    """有源素材（文档→PPT）时解析失败保持响亮失败，不静默降级丢文档上下文。"""
    node = _FakePlanNode(
        "抱歉，本助手暂时无法生成搜索建议。",
        source_material="用户上传文档的关键内容要点",
    )
    inputs = {"topic": "测试主题", "doc_raw_path": "/fake/doc.md"}

    with pytest.raises(ContentPlanError):
        await _run_p42_quick_research(node, inputs)


@pytest.mark.asyncio
async def test_p42a_contract_error_still_raises() -> None:
    """JSON 合法但契约校验失败（查询数量不符）时保持响亮失败，不静默降级。

    避免掩盖 P4.2a prompt 契约退化（LLM 返回了 JSON 但不满足 5~8 条边界）。
    """
    raw = (
        '{"entity":"金融科技","queries":['
        '{"dimension":"监管","query":"2026 金融科技 监管政策"},'
        '{"dimension":"市场","query":"2026 金融科技 市场规模"}'
        "]}"
    )
    node = _FakePlanNode(raw)
    inputs = {"topic": "测试主题"}

    with pytest.raises(ContentPlanError):
        await _run_p42_quick_research(node, inputs)
