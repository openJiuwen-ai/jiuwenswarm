# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""parse_json_payload 对流式杂散空 fence 的容错回归测试。

流式输出可能在真实 JSON fence 之前混入一个内容非 JSON 的杂散空 fence，
其闭合标记会消耗真实 JSON fence 的开始反引号，导致：
1. 首个 fence 匹配劫持提取（拿到杂散内容而非 JSON）；
2. 兜底正则 ``\\{[\\s\\S]*\\}`` 作用在「被替换后的 text」上，原始 raw 中的
   完整 JSON 无法再被定位。
本组测试确保解析器能绕过这两个缺陷正确提取 JSON。
"""

from __future__ import annotations

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon

_QUERIES_JSON = (
    '{"entity":"金融科技 FinTech","queries":['
    '{"dimension":"数字银行","query":"2026 全球数字银行 发展趋势"}]}'
)


def test_clean_json_fence_parses() -> None:
    """既有行为：干净 json fence 正常提取（不得回归）。"""
    raw = '```json\n{"material_richness":"empty"}\n```'
    assert PptCommon.parse_json_payload(raw) == {"material_richness": "empty"}


def test_plain_fence_without_lang_tag_parses() -> None:
    """既有行为：无语言标注的 fence 正常提取（不得回归）。"""
    raw = '```\n{"a": 1}\n```'
    assert PptCommon.parse_json_payload(raw) == {"a": 1}


def test_unfenced_plain_json_parses() -> None:
    """既有行为：无 fence 的纯 JSON 正文（不得回归）。"""
    assert PptCommon.parse_json_payload('{"a": 1}') == {"a": 1}


def test_unfenced_json_with_trailing_prose_uses_brace_fallback() -> None:
    """既有行为：正文 JSON 带前后散文时用大括号兜底（不得回归）。"""
    assert PptCommon.parse_json_payload('结果如下：{"a": 1} 以上。') == {"a": 1}


def test_empty_and_blank_return_none() -> None:
    """既有行为：空输入返回 None（不得回归）。"""
    assert PptCommon.parse_json_payload("") is None
    assert PptCommon.parse_json_payload("   \n  ") is None


def test_garbage_without_braces_returns_none() -> None:
    """既有行为：无任何大括号的乱码返回 None（不得回归）。"""
    assert PptCommon.parse_json_payload("抱歉，本助手暂时无法生成。") is None


def test_stray_empty_fence_hijack_still_parses() -> None:
    """杂散空 fence 劫持首个 fence 匹配后仍能解析。

    真实 JSON fence 的开始反引号被杂散 fence 的闭合标记消耗，
    fence 提取拿不到 JSON；兜底必须回退到「原始 raw」上做大括号定位。
    """
    raw = "```\n*+0\n```json\n" + _QUERIES_JSON + "\n```"
    result = PptCommon.parse_json_payload(raw)
    assert isinstance(result, dict)
    assert result["entity"] == "金融科技 FinTech"
    assert result["queries"][0]["query"] == "2026 全球数字银行 发展趋势"


def test_first_invalid_fence_falls_through_to_valid_second() -> None:
    """多 fence 场景：首个 fence 内容非法时应继续尝试后续 fence。"""
    raw = '```json\nnot-json\n```\n中间说明文字\n```json\n{"ok": true}\n```'
    assert PptCommon.parse_json_payload(raw) == {"ok": True}
