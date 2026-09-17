# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for tool_utils 内嵌引号修复（parse_json_tolerant / parse_llm_json）。

背景（2026-09-16 PPT block_002 案例）：glm-5.2 在中文段落里用未转义的
ASCII 双引号强调术语（如 ``从2024年的"稳健"到2025年的"适度宽松"``），
反馈重试后引号不减反增，导致 json.loads 稳定失败、block 标记失败、
P7 校验中止全链路 fallback。

红线（本文件逐条锁定）：
- 合法 JSON 不走修复路径，原样解析（值不变）；
- 内嵌引号按结构位置判定修复：开引号前侧是 { [ , :，闭引号后侧是 , } ] :；
- 内嵌引号成对交替替换为 “ ”，不破坏转义序列（\\"）；
- 修复后仍非法（结构性损坏）则抛 JSONDecodeError，与原行为一致；
- parse_llm_json 在 fence 剥离基础上叠加修复。

注：原文件中依赖 PPTXSumCommon（skill_codes/pptx_content_summarizer，该模块
已被移除）的 2 个用例随模块下线一并删除；修复逻辑本身由上方用例全覆盖。
"""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.skill_turbo.runtime.tool_utils import (
    _repair_inner_quotes,
    parse_json_tolerant,
    parse_llm_json,
)


def test_valid_json_untouched():
    """合法 JSON 直接解析，值不经过任何改写。"""
    payload = {"paragraphs": ["第一段", "第二段"], "count": 2}
    assert parse_json_tolerant(json.dumps(payload, ensure_ascii=False)) == payload


def test_repair_single_inner_quote_pair():
    """单个内嵌引号对（新增"+"的要求）修复后可解析。"""
    raw = '{"paragraphs": ["目标维持2%左右但新增"+"的要求"]}'
    result = parse_json_tolerant(raw)
    assert result == {"paragraphs": ["目标维持2%左右但新增“+”的要求"]}


def test_repair_multiple_inner_quote_pairs():
    """多个内嵌引号对（政策术语强调）全部成对修复。"""
    raw = (
        '{"paragraphs": ["从2024年的"稳健"到2025年的"适度宽松"再到'
        '2026年的"灵活高效"，支持"两重""两新""]}'
    )
    result = parse_json_tolerant(raw)
    text = result["paragraphs"][0]
    assert '"稳健"' not in text  # ASCII 引号形态已替换
    assert "“稳健”" in text
    assert "“适度宽松”" in text
    assert "“灵活高效”" in text
    assert "“两重”“两新”" in text


# 真实事故样本（2026-09-16 10:43:14 block_002 第二次输出，节选）：
# 段落内含多处未转义 ASCII 双引号（"稳健" "适度宽松" "两重""两新"）
_ACCIDENT_SAMPLE = (
    '{"paragraphs": ["2026年政府工作报告首次将GDP增速目标设为4.5%—5%区间。'
    '同时，CPI涨幅目标维持2%左右，货币政策从2024年的"稳健"到2025年的'
    '"适度宽松"再到2026年的"灵活高效"，超长期特别国债1.3万亿元'
    '支持"两重""两新"项目。"]}'
)


def test_repair_real_accident_sample():
    """block_002 真实事故样本（多处未转义内嵌引号）修复后可解析。"""
    with pytest.raises(json.JSONDecodeError):
        json.loads(_ACCIDENT_SAMPLE)  # 原始输出确实非法
    result = parse_json_tolerant(_ACCIDENT_SAMPLE)
    assert isinstance(result, dict)
    assert len(result["paragraphs"]) == 1
    assert "“两重”“两新”" in result["paragraphs"][0]


def test_repair_preserves_escaped_quotes():
    """合法转义序列 \\" 原样保留，不被二次改写。"""
    raw = '{"paragraphs": ["合法转义\\"保留", "内嵌"修复""]}'
    result = parse_json_tolerant(raw)
    assert result["paragraphs"][0] == '合法转义"保留'
    assert result["paragraphs"][1] == "内嵌“修复”"


def test_structural_damage_still_raises():
    """结构性损坏（截断/缺括号）修复后仍非法则抛出，与原行为一致。"""
    raw = '{"paragraphs": ["截断'  # 未闭合字符串+未闭合容器
    with pytest.raises(json.JSONDecodeError):
        parse_json_tolerant(raw)


def test_repair_inner_quotes_isolated():
    """修复函数本身：结构性引号保留，字符串外孤立引号保守替换为闭中文引号。"""
    assert _repair_inner_quotes('"a"') == '"a"'
    assert _repair_inner_quotes('["a", "b"]') == '["a", "b"]'
    # 孤立引号（前侧非结构位置）替换，不引入结构变化
    assert _repair_inner_quotes("a”b") == "a”b"
    assert _repair_inner_quotes('a"b') == "a”b"


def test_parse_llm_json_with_fence_and_repair():
    """parse_llm_json：fence 剥离 + 内嵌引号修复叠加生效。"""
    raw = '```json\n{"paragraphs": ["他说"灵活高效"取代此前"]}\n```'
    result = parse_llm_json(raw)
    assert result == {"paragraphs": ["他说“灵活高效”取代此前"]}
