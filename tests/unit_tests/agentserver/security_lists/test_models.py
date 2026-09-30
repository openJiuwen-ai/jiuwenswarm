# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""security_lists.models 单测：type/match 组合、cells 键空间、唯一性、resolve_cell 回退链。"""
from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.rails.security_lists.models import (
    DuplicateRecordError,
    SecurityListRecord,
    record_from_dict,
    record_to_dict,
    resolve_cell,
    resolve_default,
    validate_defaults,
    validate_record,
)


def rec(**kw) -> SecurityListRecord:
    base = dict(
        id="ul_aaaaaaaa",
        type="file_path",
        pattern="C:/data/**",
        match="glob",
        cells={"*": {"read": "allow"}},
    )
    base.update(kw)
    return SecurityListRecord(**base)


@pytest.mark.parametrize("list_type,match,pattern,cells", [
    ("file_path", "glob", "C:/a/**", {"*": {"read": "allow"}}),
    ("file_path", "prefix", "C:/a", {"*": {"write": "ask", "exec": "deny", "*": "allow"}}),
    ("domain", "exact", "example.com", {"*": {"*": "allow"}}),
    ("domain", "wildcard", "*.example.com", {"*": {"*": "deny"}}),
    ("command", "exact", "rm -rf /", {"*": {"*": "deny"}}),
    ("command", "glob", "git *", {"*": {"*": "ask"}}),
    ("command", "regex", r"curl\s+.*", {"*": {"*": "deny"}}),
])
def test_legal_type_match_combos(list_type, match, pattern, cells):
    validate_record(rec(type=list_type, match=match, pattern=pattern, cells=cells))


@pytest.mark.parametrize("list_type,match", [
    ("file_path", "exact"), ("file_path", "wildcard"), ("file_path", "regex"),
    ("domain", "glob"), ("domain", "prefix"), ("domain", "regex"),
    ("command", "prefix"), ("command", "wildcard"),
])
def test_illegal_type_match_combos(list_type, match):
    with pytest.raises(ValueError, match="不支持 match"):
        validate_record(rec(type=list_type, match=match))


def test_unknown_list_type():
    with pytest.raises(ValueError, match="未知名单类型"):
        validate_record(rec(type="registry"))


@pytest.mark.parametrize("cells", [
    {"strict": {"read": "allow"}},            # 未知模式键
    {"*": {"delete": "allow"}},               # file_path 不支持的操作键
    {"*": {"read": "block"}},                 # 未知格子值
    {"default": "allow"},                     # 行不是映射
])
def test_invalid_cells(cells):
    with pytest.raises(ValueError):
        validate_record(rec(cells=cells))


def test_domain_rejects_file_ops():
    with pytest.raises(ValueError, match="不支持操作键"):
        validate_record(rec(type="domain", match="exact", pattern="example.com",
                            cells={"*": {"read": "allow"}}))


def test_empty_cells_is_legal():
    validate_record(rec(cells={}))


@pytest.mark.parametrize("pattern", ["", "   "])
def test_empty_pattern(pattern):
    with pytest.raises(ValueError, match="pattern 不能为空"):
        validate_record(rec(pattern=pattern))


@pytest.mark.parametrize("pattern", ["https://example.com", "example.com/path",
                                     "example.com:8080", "example.com?x=1"])
def test_domain_pattern_rejects_scheme_path_port(pattern):
    with pytest.raises(ValueError, match="domain pattern"):
        validate_record(rec(type="domain", match="exact", pattern=pattern, cells={"*": {"*": "allow"}}))


def test_wildcard_domain_must_start_with_star_dot():
    with pytest.raises(ValueError, match="wildcard domain"):
        validate_record(rec(type="domain", match="wildcard", pattern="example.com",
                            cells={"*": {"*": "allow"}}))


def test_exact_domain_rejects_asterisk():
    with pytest.raises(ValueError, match="exact domain"):
        validate_record(rec(type="domain", match="exact", pattern="*.example.com",
                            cells={"*": {"*": "allow"}}))


def test_command_regex_must_compile():
    with pytest.raises(ValueError, match="regex 不可编译"):
        validate_record(rec(type="command", match="regex", pattern="[unclosed",
                            cells={"*": {"*": "deny"}}))


def test_enabled_must_be_bool():
    with pytest.raises(ValueError, match="enabled 须为布尔"):
        validate_record(rec(enabled="yes"))


def test_duplicate_object_raises():
    other = rec(id="ul_bbbbbbbb")
    with pytest.raises(DuplicateRecordError, match="操作对象已存在"):
        validate_record(rec(), existing=[other])


def test_same_id_is_not_duplicate():
    validate_record(rec(), existing=[rec()])  # 同 id 视为自身更新


def test_different_pattern_is_not_duplicate():
    validate_record(rec(), existing=[rec(id="ul_bbbbbbbb", pattern="D:/other")])


# ---------------------------------------------------------------------------
# resolve_cell 回退链
# ---------------------------------------------------------------------------

CELLS = {
    "default": {"read": "deny", "*": "ask"},
    "*": {"read": "allow", "*": "allow"},
}


@pytest.mark.parametrize("mode,op,expected", [
    ("default", "read", "deny"),        # cells[mode][op] 最具体
    ("default", "write", "ask"),        # cells[mode]["*"]
    ("auto_approve", "read", "allow"),  # cells["*"][op]
    ("auto_approve", "exec", "allow"),  # cells["*"]["*"]
    ("full_access", "write", "allow"),
    ("*", "read", "allow"),             # mode="*" 直查通用格
])
def test_resolve_cell_fallback_chain(mode, op, expected):
    assert resolve_cell(CELLS, mode, op) == expected


@pytest.mark.parametrize("cells,mode,op", [
    ({}, "default", "read"),
    ({"default": {}}, "auto_approve", "read"),
    ({"default": {"read": "deny"}}, "auto_approve", "read"),  # 模式特化不跨模式
    ({"*": {"write": "deny"}}, "*", "read"),
])
def test_resolve_cell_all_empty_returns_none(cells, mode, op):
    assert resolve_cell(cells, mode, op) is None


def test_mode_specific_does_not_leak_to_other_modes():
    cells = {"full_access": {"read": "allow"}}
    assert resolve_cell(cells, "default", "read") is None
    assert resolve_cell(cells, "full_access", "read") == "allow"


# ---------------------------------------------------------------------------
# 序列化
# ---------------------------------------------------------------------------


def test_record_round_trip():
    original = rec(cells={"default": {"read": "deny"}, "*": {"*": "ask"}},
                   note="测试备注", migrated_from="sandbox_copy",
                   created_at="2026-09-28T00:00:00+00:00",
                   updated_at="2026-09-28T01:00:00+00:00")
    restored = record_from_dict(record_to_dict(original))
    assert restored == original


def test_record_to_dict_omits_empty_optional_fields():
    data = record_to_dict(rec())
    assert "note" not in data
    assert "migrated_from" not in data


@pytest.mark.parametrize("data", [
    "not-a-dict",
    {"type": "file_path", "match": "glob"},                    # 缺 pattern
    {"type": "file_path", "pattern": "x", "match": "glob", "enabled": "yes"},
    {"type": "file_path", "pattern": "x", "match": "glob", "cells": ["read"]},
    {"type": "file_path", "pattern": "x", "match": "glob", "cells": {"*": "allow"}},
])
def test_record_from_dict_malformed(data):
    with pytest.raises(ValueError):
        record_from_dict(data)


def test_record_from_dict_ignores_unknown_keys():
    restored = record_from_dict({"type": "domain", "pattern": "example.com", "match": "exact",
                                 "future_field": {"x": 1}})
    assert restored.type == "domain"
    assert restored.source == "user"  # 默认来源


def test_record_from_dict_default_source():
    restored = record_from_dict({"type": "domain", "pattern": "example.com", "match": "exact"},
                                default_source="cloud")
    assert restored.source == "cloud"


# ---------------------------------------------------------------------------
# v3 兜底档 defaults
# ---------------------------------------------------------------------------


def test_validate_defaults_accepts_legal_keyspace():
    validate_defaults({})                                              # 无兜底＝保持现状
    validate_defaults({"*": {"domain": "deny", "file_path": "allow", "command": "ask"}})
    validate_defaults({"*": {"*": "allow"}})                           # 通用格：所有类型
    validate_defaults({"full_access": {"domain": "allow"}})            # 模式特化格


@pytest.mark.parametrize("defaults", [
    "deny",                              # 整体不是映射
    {"*": "deny"},                       # 模式行不是映射
    {"weird_mode": {"domain": "deny"}},  # 未知模式键
    {"*": {"ip": "deny"}},               # 未知类型键
    {"*": {"domain": "block"}},          # 非法动作
])
def test_validate_defaults_rejects_illegal(defaults):
    with pytest.raises(ValueError):
        validate_defaults(defaults)


def test_resolve_default_fallback_chain():
    # defaults[mode][type] → defaults[mode]["*"] → defaults["*"][type] → defaults["*"]["*"]
    defaults = {"*": {"*": "allow", "domain": "deny"}}
    assert resolve_default(defaults, "default", "domain") == "deny"        # 类型特化格
    assert resolve_default(defaults, "default", "file_path") == "allow"    # 落到通用格
    assert resolve_default({}, "default", "domain") is None                # 无兜底＝无表态
    # 模式隔离：别的模式的格子不泄漏
    assert resolve_default({"full_access": {"domain": "allow"}}, "default", "domain") is None
    # 通用模式格对所有模式命中
    assert resolve_default({"*": {"domain": "deny"}}, "full_access", "domain") == "deny"
