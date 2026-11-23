# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""资产统一导入导出契约层单测。"""

from __future__ import annotations

import pytest

from jiuwenswarm.server.runtime.assets.contract import (
    AssetKind,
    build_export_filename,
    normalize_asset_kind,
    parse_export_params,
    parse_import_params,
    sanitize_export_stem,
    validate_asset_id,
)


def test_req_method_members_registered() -> None:
    """assets.export / assets.import 已注册进 ReqMethod。"""
    from jiuwenswarm.common.schema.message import ReqMethod

    assert ReqMethod.ASSETS_EXPORT.value == "assets.export"
    assert ReqMethod.ASSETS_IMPORT.value == "assets.import"


def test_kind_enum_is_fixed_five_kinds() -> None:
    """kind 枚举固定五类、与发布体系一致，禁止自造命名。"""
    assert [kind.value for kind in AssetKind] == [
        "skill",
        "skillpack",
        "agent_template",
        "agent_group",
        "plugin",
    ]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("skill", AssetKind.SKILL),
        ("skills", AssetKind.SKILL),
        ("skillpack", AssetKind.SKILLPACK),
        ("skillpacks", AssetKind.SKILLPACK),
        ("agent_template", AssetKind.AGENT_TEMPLATE),
        ("agent_templates", AssetKind.AGENT_TEMPLATE),
        ("agent-template", AssetKind.AGENT_TEMPLATE),
        ("agent-templates", AssetKind.AGENT_TEMPLATE),
        ("agent_group", AssetKind.AGENT_GROUP),
        ("agent_groups", AssetKind.AGENT_GROUP),
        ("agent-group", AssetKind.AGENT_GROUP),
        ("plugin", AssetKind.PLUGIN),
        ("plugins", AssetKind.PLUGIN),
        ("plugin_packages", AssetKind.PLUGIN),
        ("agent-plugin", AssetKind.PLUGIN),
        ("  Agent_Template  ", AssetKind.AGENT_TEMPLATE),
        (AssetKind.SKILL, AssetKind.SKILL),
    ],
)
def test_normalize_asset_kind_accepts_known_spellings(raw, expected) -> None:
    assert normalize_asset_kind(raw) is expected


@pytest.mark.parametrize("raw", ["", "expert", "mcp", "agent", None, 42, ["skill"]])
def test_normalize_asset_kind_rejects_unknown(raw) -> None:
    with pytest.raises(ValueError):
        normalize_asset_kind(raw)


@pytest.mark.parametrize(
    "asset_id", ["my-expert", "bond_analyst", "债券投研专家", "v1.2", "a b"]
)
def test_validate_asset_id_accepts_plain_names(asset_id) -> None:
    assert validate_asset_id(asset_id) == asset_id


@pytest.mark.parametrize(
    "asset_id",
    [
        "",
        "   ",
        ".",
        "..",
        "a/b",
        "a\\b",
        "../escape",
        "bad\x00id",
        "bad\nid",
        # 文件名特殊字符：id 必须与顶层目录名严格一致且一一对应，
        # 交给 sanitize 做有损清洗会把多个不同 id 映射到同一文件名。
        "a*b",
        'a?b',
        'a"b',
        "a<b>c",
        "a|b",
        "a:b",
        42,
        None,
    ],
)
def test_validate_asset_id_rejects_unsafe(asset_id) -> None:
    with pytest.raises(ValueError):
        validate_asset_id(asset_id)


def test_build_export_filename() -> None:
    """文件名 <id>[-<version>].zip；version 残留危险字符时安全清洗。"""
    assert build_export_filename("my-expert") == "my-expert.zip"
    assert build_export_filename("my-expert", "1.2.0") == "my-expert-1.2.0.zip"
    assert build_export_filename("my-expert", "beta/1") == "my-expert-beta_1.zip"
    # sanitize 是 version 等自由字段的兜底清洗；id 自身由 validate_asset_id
    # 保证不含特殊字符（一一对应），不再依赖有损映射。
    assert sanitize_export_stem('a:b*c?"d') == "a_b_c_d"
    assert sanitize_export_stem("lead..trail..") == "lead..trail"


def test_build_export_filename_rejects_degenerate() -> None:
    with pytest.raises(ValueError):
        build_export_filename("***")  # id 含特殊字符，直接拒绝
    with pytest.raises(ValueError):
        build_export_filename("ok", " ")  # version 清洗后为空
    with pytest.raises(ValueError):
        build_export_filename("../escape")


def test_parse_export_params_normalizes() -> None:
    parsed = parse_export_params(
        {"kind": "agent_templates", "local_id": " bond-analyst ", "version": " 1.0 "}
    )
    assert parsed == {
        "kind": AssetKind.AGENT_TEMPLATE,
        "local_id": "bond-analyst",
        "version": "1.0",
    }
    parsed = parse_export_params({"kind": "skill", "local_id": "web-search"})
    assert parsed == {"kind": AssetKind.SKILL, "local_id": "web-search"}


@pytest.mark.parametrize(
    "params",
    [
        None,
        "skill",
        {},  # 缺 kind
        {"kind": "skill"},  # 缺 local_id
        {"kind": "skill", "local_id": "  "},
        {"kind": "expert", "local_id": "x"},  # 未知 kind
        {"kind": "skill", "local_id": "x", "version": 42},
        {"kind": "skill", "local_id": "x", "version": ""},
        # 路径穿越片段必须在解析阶段拒绝：适配器按 (kind, local_id)
        # 定位本地包目录，穿越值不能等到达文件名/目录拼接时才暴露。
        {"kind": "skill", "local_id": "../escape"},
        {"kind": "skill", "local_id": "a/b"},
        {"kind": "skill", "local_id": "a*b"},
        # items 为协议预留的批量字段，一期显式拒绝而非静默忽略
        {"kind": "skill", "local_id": "x", "items": [{"kind": "skill", "local_id": "y"}]},
    ],
)
def test_parse_export_params_rejects_invalid(params) -> None:
    with pytest.raises(ValueError):
        parse_export_params(params)


def test_parse_import_params_kind_optional() -> None:
    parsed = parse_import_params({"path": "/tmp/uploads/pkg.zip"})
    assert parsed == {"path": "/tmp/uploads/pkg.zip"}
    parsed = parse_import_params({"path": " /tmp/x.zip ", "kind": "plugin_packages"})
    assert parsed == {"path": "/tmp/x.zip", "kind": AssetKind.PLUGIN}


@pytest.mark.parametrize(
    "params",
    [None, {}, {"path": ""}, {"path": 42}, {"path": "/x.zip", "kind": "expert"}],
)
def test_parse_import_params_rejects_invalid(params) -> None:
    with pytest.raises(ValueError):
        parse_import_params(params)


@pytest.mark.parametrize(
    "params",
    [
        {"path": "../../etc/passwd"},
        {"path": "a/../b.zip"},
        {"path": ".."},
        {"path": "a\\b.zip"},  # 反斜杠分隔符变体
        {"path": "pkg.zip\x00"},
    ],
)
def test_parse_import_params_rejects_unsafe_paths(params) -> None:
    """契约层挡语法穿越；受控根收敛由导入服务 resolve 负责（分层防御）。"""
    with pytest.raises(ValueError):
        parse_import_params(params)


def test_parse_import_params_allows_absolute_and_relative_names() -> None:
    """绝对路径与纯相对文件名语法合法；越权落点由导入服务收敛。"""
    assert parse_import_params({"path": "/tmp/uploads/pkg.zip"}) == {
        "path": "/tmp/uploads/pkg.zip"
    }
    assert parse_import_params({"path": "uploads/pkg.zip"}) == {"path": "uploads/pkg.zip"}
