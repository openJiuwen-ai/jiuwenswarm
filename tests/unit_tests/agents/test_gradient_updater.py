# -*- coding: utf-8 -*-
"""gradient_updater 单元测试。

该模块（推荐策略梯度更新：LLM 反馈 → 规则操作流 → apply_operations）此前零测试。
用例分四组：

- apply_operations：add/revise/drop 语义 + 畸形操作逐条容错（单条畸形不连累整批，
  同批 gid 不碰撞，revise 空值不覆盖原规则，存量脏梯度不炸）；
- _parse_operations / _extract_json_from_response / _extract_output_text：LLM 输出
  解析边界；
- attribute_gradients / render_decision_rules / render_style_rules：分层归因与渲染；
- _render_feedbacks：反馈渲染回归。

异步 LLM 流程（update_gradients 及两个 _update_gradients_from_*）属管道胶水，
其可测逻辑（解析 + 应用）已由纯函数用例覆盖。
"""

from __future__ import annotations

import json

import jiuwenswarm.agents.harness.common.recommendation.gradient_updater as gu
from jiuwenswarm.agents.harness.common.recommendation.gradient_updater import (
    _extract_json_from_response,
    _extract_output_text,
    _parse_operations,
    _render_feedbacks,
    apply_operations,
    attribute_gradients,
    render_decision_rules,
    render_style_rules,
)


def _g(gid: str, rule: str, category: str = "target") -> dict:
    return {"gradient_id": gid, "rule": rule, "category": category}


# ---------- apply_operations：add ----------


def test_add_creates_gradient_with_prefix() -> None:
    result = apply_operations(
        [_g("g1", "旧规则")],
        [
            {"action": "add", "rule": "新规则", "category": "tone"},
        ],
    )
    added = [g for g in result if g["rule"] == "新规则"]
    assert len(added) == 1
    assert added[0]["gradient_id"].startswith("g_")
    assert added[0]["category"] == "tone"


def test_add_empty_rule_skipped() -> None:
    result = apply_operations(
        [],
        [
            {"action": "add", "rule": "", "category": "target"},
            {"action": "add", "rule": "   ", "category": "target"},
        ],
    )
    assert result == []


def test_add_non_string_rule_skipped_without_killing_batch() -> None:
    """单条畸形 add（rule 非 str）不得连累同批合法操作。"""
    result = apply_operations(
        [],
        [
            {"action": "add", "rule": 123, "category": "target"},
            {"action": "add", "rule": "合法规则", "category": "target"},
        ],
    )
    rules = [g["rule"] for g in result]
    assert "合法规则" in rules
    assert 123 not in rules


def test_add_null_rule_skipped() -> None:
    result = apply_operations(
        [], [{"action": "add", "rule": None, "category": "target"}]
    )
    assert result == []


def test_add_gid_unique_within_same_batch(monkeypatch) -> None:
    """同一毫秒内 add→drop→add：两条新规则都必须保留，gid 不得碰撞。"""
    monkeypatch.setattr(gu.time, "time", lambda: 1700000000.0)
    result = apply_operations(
        [_g("g1", "存量")],
        [
            {"action": "add", "rule": "规则甲", "category": "target"},
            {"action": "drop", "gradient_id": "g1"},
            {"action": "add", "rule": "规则乙", "category": "target"},
        ],
    )
    rules = [g["rule"] for g in result]
    assert "规则甲" in rules and "规则乙" in rules
    gids = [g["gradient_id"] for g in result]
    assert len(gids) == len(set(gids))


def test_add_multiple_gid_unique_same_ms(monkeypatch) -> None:
    monkeypatch.setattr(gu.time, "time", lambda: 1700000000.0)
    result = apply_operations(
        [],
        [{"action": "add", "rule": f"规则{i}", "category": "target"} for i in range(5)],
    )
    gids = [g["gradient_id"] for g in result]
    assert len(gids) == len(set(gids)) == 5


# ---------- apply_operations：revise ----------


def test_revise_updates_rule_and_category() -> None:
    result = apply_operations(
        [_g("g1", "旧规则", "tone")],
        [
            {
                "action": "revise",
                "gradient_id": "g1",
                "rule": "新规则",
                "category": "target",
            },
        ],
    )
    assert result == [{"gradient_id": "g1", "rule": "新规则", "category": "target"}]


def test_revise_moves_to_end() -> None:
    """revise 后挪到末尾（新近度语义，配合 [-N:] 截断）。"""
    result = apply_operations(
        [_g("g1", "一"), _g("g2", "二"), _g("g3", "三")],
        [{"action": "revise", "gradient_id": "g1", "rule": "一改"}],
    )
    assert [g["gradient_id"] for g in result] == ["g2", "g3", "g1"]


def test_revise_null_rule_keeps_original() -> None:
    """LLM 返回 rule: null 不得把原规则覆盖成 None。"""
    result = apply_operations(
        [_g("g1", "原规则")],
        [
            {"action": "revise", "gradient_id": "g1", "rule": None},
        ],
    )
    assert result[0]["rule"] == "原规则"


def test_revise_empty_rule_noop() -> None:
    result = apply_operations(
        [_g("g1", "原规则")],
        [
            {"action": "revise", "gradient_id": "g1", "rule": "  "},
        ],
    )
    assert result[0]["rule"] == "原规则"


def test_revise_non_string_category_ignored() -> None:
    result = apply_operations(
        [_g("g1", "规则", "tone")],
        [
            {"action": "revise", "gradient_id": "g1", "category": 42},
        ],
    )
    assert result[0]["category"] == "tone"


def test_revise_unknown_gid_ignored() -> None:
    result = apply_operations(
        [_g("g1", "规则")],
        [
            {"action": "revise", "gradient_id": "ghost", "rule": "幻觉规则"},
        ],
    )
    assert result == [_g("g1", "规则")]


# ---------- apply_operations：drop ----------


def test_drop_removes_gradient() -> None:
    result = apply_operations(
        [_g("g1", "一"), _g("g2", "二")],
        [
            {"action": "drop", "gradient_id": "g1"},
        ],
    )
    assert [g["gradient_id"] for g in result] == ["g2"]


def test_drop_unknown_gid_ignored() -> None:
    result = apply_operations(
        [_g("g1", "一")], [{"action": "drop", "gradient_id": "ghost"}]
    )
    assert len(result) == 1


# ---------- apply_operations：存量脏梯度容错 ----------


def test_malformed_gradient_missing_gid_preserved_not_fatal() -> None:
    """存量梯度缺 gradient_id（持久化脏数据）不得 KeyError，且不被静默删除。"""
    dirty = {"rule": "无id的存量规则", "category": "tone"}
    result = apply_operations(
        [dirty],
        [
            {"action": "add", "rule": "新规则", "category": "target"},
        ],
    )
    rules = [g["rule"] for g in result]
    assert "无id的存量规则" in rules and "新规则" in rules


def test_malformed_gradient_non_dict_preserved() -> None:
    result = apply_operations(
        [_g("g1", "正常"), "garbage"],  # type: ignore[list-item]
        [{"action": "add", "rule": "新规则", "category": "target"}],
    )
    assert any(g == "garbage" for g in result)
    assert any(isinstance(g, dict) and g["rule"] == "新规则" for g in result)


def test_non_dict_operation_skipped() -> None:
    """操作列表里混入非 dict 项：跳过该项，其余正常应用。"""
    result = apply_operations(
        [],
        [
            "not-an-op",  # type: ignore[list-item]
            {"action": "add", "rule": "合法规则", "category": "target"},
        ],
    )
    assert [g["rule"] for g in result] == ["合法规则"]


# ---------- _parse_operations：LLM 载荷防线 ----------


def test_parse_operations_valid() -> None:
    data = json.loads('{"operations": [{"action": "add", "rule": "r"}]}')
    assert _parse_operations(data) == [{"action": "add", "rule": "r"}]


def test_parse_operations_guards() -> None:
    assert _parse_operations([]) == []  # 载荷是数组
    assert _parse_operations("str") == []  # 载荷是字符串
    assert _parse_operations({}) == []  # 无 operations
    assert _parse_operations({"operations": None}) == []
    assert _parse_operations({"operations": {"a": 1}}) == []  # 不是列表
    assert _parse_operations(
        {"operations": ["junk", {"action": "drop", "gradient_id": "g1"}]}
    ) == [{"action": "drop", "gradient_id": "g1"}]  # 非 dict 项被过滤


# ---------- LLM 输出提取（回归：现有正确行为） ----------


def test_extract_json_fenced_and_raw() -> None:
    assert _extract_json_from_response('```json\n{"a": 1}\n```') == '{"a": 1}'
    assert _extract_json_from_response('```\n{"a": 2}\n```') == '{"a": 2}'
    assert _extract_json_from_response('前置文本 {"a": 3} 尾部文本') == '{"a": 3}'
    assert _extract_json_from_response('{"a": 4} 尾部 }{ 混淆') == '{"a": 4}'
    assert _extract_json_from_response("没有 JSON") == ""
    assert _extract_json_from_response("") == ""


def test_extract_output_text_variants() -> None:
    assert _extract_output_text(None) == ""
    assert _extract_output_text("plain") == "plain"
    assert _extract_output_text({"output": "str"}) == "str"
    assert _extract_output_text({"output": ["a", "b", 3]}) == "a\nb"
    assert _extract_output_text({"output": 42}) == ""
    assert _extract_output_text({"other": "x"}) == ""


# ---------- 归因与渲染 ----------


def test_attribute_gradients_layers() -> None:
    gradients = [
        _g("g1", "r1", "target"),
        _g("g2", "r2", "relation"),
        _g("g3", "r3", "tone"),
        _g("g4", "r4", "structure"),
        _g("g5", "r5", "unknown-cat"),
    ]
    decision, style = attribute_gradients(gradients)
    assert [g["gradient_id"] for g in decision] == ["g1", "g2", "g5"]  # 未知类归决策层
    assert [g["gradient_id"] for g in style] == ["g3", "g4"]


def test_attribute_gradients_non_dict_skipped() -> None:
    decision, style = attribute_gradients([_g("g1", "r"), "junk"])
    assert len(decision) == 1 and style == []


def test_render_gradients_skips_non_dict() -> None:
    from jiuwenswarm.agents.harness.common.recommendation.gradient_updater import (
        _render_gradients,
    )

    text = _render_gradients([_g("g1", "正常规则", "target"), "junk"])
    assert "正常规则" in text and "junk" not in text


def test_render_decision_rules_latest_ten_and_empty() -> None:
    gradients = [_g(f"g{i}", f"规则{i}", "target") for i in range(15)]
    text = render_decision_rules(gradients)
    assert "规则14" in text and "规则5" in text
    assert "规则4" not in text  # 只留最新 10 条
    assert render_decision_rules([], language="zh") == "（暂无）"
    assert render_decision_rules([], language="en") == "(none yet)"


def test_render_style_rules_heading_and_latest_ten() -> None:
    assert render_style_rules([]) == ""
    gradients = [_g(f"g{i}", f"风格{i}", "tone") for i in range(12)]
    text = render_style_rules(gradients, language="zh")
    assert text.startswith("【话术风格要求】")
    assert "风格11" in text and "风格1" in text and "风格0" not in text


def test_render_feedbacks_zh_en_empty() -> None:
    assert _render_feedbacks([], "zh") == "（无反馈）"
    assert _render_feedbacks([], "en") == "(no feedback)"
    zh = _render_feedbacks(
        [
            {
                "feedback_type": "explicit_like",
                "rec_content": "翻译技能",
                "user_reply": "谢谢",
            }
        ],
        "zh",
    )
    assert "反馈类型: explicit_like" in zh and "推荐内容: 翻译技能" in zh
    en = _render_feedbacks(
        [{"feedback_type": "implicit", "user_reply": "not needed"}], "en"
    )
    assert "feedback type: implicit" in en and "user reply: not needed" in en
