# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the conflict-free built-in tool spec discovery registry."""

from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

import pytest

from jiuwenswarm.agents.harness.common.tools.builtin_tool_registry import (
    BuiltinToolSpec,
    clear_registered_tool_specs,
    discover_builtin_tool_specs,
    discover_lazy_exports,
    discover_permission_sets,
    merge_discovered_categories,
    register_builtin_tool_spec,
)


class _DummyCategory(NamedTuple):
    path: str
    description: str
    tools: tuple[str, ...]
    description_en: str = ""


@pytest.fixture(autouse=True)
def _reset_registry():
    clear_registered_tool_specs()
    yield
    clear_registered_tool_specs()


def test_ast_discovery_from_tool_modules_and_specs_dir(tmp_path: Path) -> None:
    """Verify AST-based discovery reads *_tools.py and specs/*.py without importing."""
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "alpha_spec.py").write_text(
        "BUILTIN_TOOL_SPECS = (\n"
        "    BuiltinToolSpec(\n"
        "        name='alpha_tool',\n"
        "        module='.alpha_tools',\n"
        "        display_en='Alpha analysis tool.',\n"
        "        aliases_zh='阿尔法 分析',\n"
        "        category_path='code-analysis',\n"
        "        permission_scope='path_read',\n"
        "    ),\n"
        ")\n",
        encoding="utf-8",
    )
    (tmp_path / "beta_tools.py").write_text(
        "# Module with unimportable third-party import to prove AST-only discovery\n"
        "import non_existent_heavy_dependency_xyz\n\n"
        "BUILTIN_TOOL_SPECS = [\n"
        "    {\n"
        "        'name': 'beta_writer',\n"
        "        'display_en': 'Beta writer tool.',\n"
        "        'aliases_zh': ('贝塔', '写入'),\n"
        "        'category_path': 'custom-cat',\n"
        "        'category_description': '自定义分类描述',\n"
        "        'category_description_en': 'Custom category description',\n"
        "        'permission_scope': 'path_write',\n"
        "    }\n"
        "]\n",
        encoding="utf-8",
    )

    discovered = discover_builtin_tool_specs(tmp_path)
    by_name = {spec.name: spec for spec in discovered}
    assert set(by_name.keys()) == {"alpha_tool", "beta_writer"}

    alpha = by_name["alpha_tool"]
    assert alpha.module == ".alpha_tools"
    assert alpha.resolved_symbol() == "alpha_tool"
    assert alpha.aliases_zh == ("阿尔法", "分析")
    assert alpha.permission_scope == "path_read"

    beta = by_name["beta_writer"]
    assert beta.module == ".beta_tools"
    assert beta.aliases_zh == ("贝塔", "写入")
    assert beta.permission_scope == "path_write"


def test_discover_lazy_exports_and_categories(tmp_path: Path) -> None:
    """Verify lazy export mapping and category merging preserve existing items."""
    (tmp_path / "gamma_tools.py").write_text(
        "BUILTIN_TOOL_SPECS = (\n"
        "    BuiltinToolSpec(\n"
        "        name='gamma_doc',\n"
        "        display_en='Read gamma docs.',\n"
        "        category_path='documents',\n"
        "    ),\n"
        "    BuiltinToolSpec(\n"
        "        name='delta_net',\n"
        "        symbol='delta_fn',\n"
        "        display_en='Fetch delta.',\n"
        "        category_path='net-tools',\n"
        "        category_description='网络工具集',\n"
        "        category_description_en='Network tools',\n"
        "        permission_scope='public_web_read',\n"
        "    ),\n"
        ")\n",
        encoding="utf-8",
    )

    existing = {"read_pdf": (".pdf_tools", "read_pdf")}
    added = discover_lazy_exports(existing, tmp_path)
    assert added == {
        "gamma_doc": (".gamma_tools", "gamma_doc"),
        "delta_net": (".gamma_tools", "delta_fn"),
    }

    base_cats = (
        _DummyCategory(
            path="documents",
            description="文档工具",
            tools=("read_pdf",),
            description_en="Document tools",
        ),
    )
    merged = merge_discovered_categories(base_cats, _DummyCategory, tmp_path)
    assert len(merged) == 2
    assert merged[0].path == "documents"
    assert merged[0].tools == ("read_pdf", "gamma_doc")
    assert merged[1].path == "net-tools"
    assert merged[1].tools == ("delta_net",)
    assert merged[1].description == "网络工具集"
    assert merged[1].description_en == "Network tools"


def test_discover_permission_sets_and_programmatic_registration(tmp_path: Path) -> None:
    """Verify permission scope grouping and programmatic spec validation."""
    (tmp_path / "perm_tools.py").write_text(
        "BUILTIN_TOOL_SPECS = (\n"
        "    BuiltinToolSpec(name='r_tool', module='.p', permission_scope='path_read'),\n"
        "    BuiltinToolSpec(name='w_tool', module='.p', permission_scope='path_write'),\n"
        "    BuiltinToolSpec(name='web_tool', module='.p', permission_scope='public_web_read'),\n"
        "    BuiltinToolSpec(name='ro_tool', module='.p', permission_scope='internal_readonly'),\n"
        ")\n",
        encoding="utf-8",
    )
    perms = discover_permission_sets(tmp_path)
    assert perms["path_read"] == frozenset({"r_tool"})
    assert perms["path_write"] == frozenset({"w_tool"})
    assert perms["public_web_read"] == frozenset({"web_tool"})
    assert perms["internal_readonly"] == frozenset({"ro_tool"})

    spec = BuiltinToolSpec(name="prog_tool", module=".prog_tools", display_en="Prog")
    register_builtin_tool_spec(spec)
    assert any(s.name == "prog_tool" for s in discover_builtin_tool_specs())

    with pytest.raises(ValueError, match="non-empty name and module"):
        register_builtin_tool_spec(BuiltinToolSpec(name="", module=".x"))
    with pytest.raises(ValueError, match="Unsupported permission_scope"):
        register_builtin_tool_spec(
            BuiltinToolSpec(name="bad", module=".x", permission_scope="invalid_scope")
        )


def test_malformed_or_invalid_spec_files_are_ignored_gracefully(tmp_path: Path) -> None:
    """Verify syntax errors or invalid spec nodes do not crash discovery."""
    (tmp_path / "broken_tools.py").write_text("def broken(:\n", encoding="utf-8")
    (tmp_path / "weird_tools.py").write_text(
        "BUILTIN_TOOL_SPECS = (\n"
        "    {'name': 'bad_scope', 'permission_scope': 'not_valid'},\n"
        "    OtherCall(name='ignored'),\n"
        "    123,\n"
        ")\n",
        encoding="utf-8",
    )
    assert discover_builtin_tool_specs(tmp_path) == ()


def test_gitattributes_configures_union_merge_for_doc_indexes() -> None:
    """Verify .gitattributes enables union merge on generated INDEX.md files."""
    repo_root = Path(__file__).resolve().parents[3]
    gitattributes = (repo_root / ".gitattributes").read_text(encoding="utf-8")
    assert ".agents/agent-docs/INDEX.md merge=union" in gitattributes
    assert ".agents/agent-docs/*/INDEX.md merge=union" in gitattributes


def test_specs_dir_default_module_includes_specs_package(tmp_path: Path) -> None:
    specs_dir = tmp_path / "specs"
    specs_dir.mkdir()
    (specs_dir / "beta_spec.py").write_text(
        "BUILTIN_TOOL_SPECS = ({'name': 'beta_tool'},)\n",
        encoding="utf-8",
    )
    found = {s.name: s for s in discover_builtin_tool_specs(tmp_path)}
    assert found["beta_tool"].module == ".specs.beta_spec"


def test_positional_args_are_mapped_and_unsupported_forms_are_skipped(tmp_path: Path) -> None:
    (tmp_path / "pos_tools.py").write_text(
        "BUILTIN_TOOL_SPECS = (\n"
        "    BuiltinToolSpec('pos_tool', '.pos_tools', 'run_pos'),\n"
        "    BuiltinToolSpec('kw_tool', module='.pos_tools'),\n"
        "    registry.BuiltinToolSpec(name='attr_tool', module='.pos_tools'),\n"
        "    BuiltinToolSpec(name=compute(), module='.pos_tools'),\n"
        ")\n",
        encoding="utf-8",
    )
    found = {s.name: s for s in discover_builtin_tool_specs(tmp_path)}
    assert set(found) == {"pos_tool", "kw_tool"}
    assert found["pos_tool"].resolved_symbol() == "run_pos"


def test_permission_sets_lowercase_names_and_respect_exclude(tmp_path: Path) -> None:
    (tmp_path / "perm_tools.py").write_text(
        "BUILTIN_TOOL_SPECS = (\n"
        "    {'name': 'Mixed_Case', 'permission_scope': 'path_write'},\n"
        "    {'name': 'existing_tool', 'permission_scope': 'path_write'},\n"
        ")\n",
        encoding="utf-8",
    )
    sets = discover_permission_sets(tmp_path, exclude=frozenset({"existing_tool"}))
    assert sets["path_write"] == frozenset({"mixed_case"})

def test_files_without_marker_are_skipped_and_category_without_description_is_added(
    tmp_path: Path,
) -> None:
    (tmp_path / "plain_tools.py").write_text("def plain():\n    return 1\n", encoding="utf-8")
    (tmp_path / "cat_tools.py").write_text(
        "BUILTIN_TOOL_SPECS = ({'name': 'cat_tool', 'category_path': 'cat-new'},)\n",
        encoding="utf-8",
    )
    assert [s.name for s in discover_builtin_tool_specs(tmp_path)] == ["cat_tool"]
    merged = merge_discovered_categories((), _DummyCategory, tmp_path)
    assert merged[0].path == "cat-new" and merged[0].tools == ("cat_tool",)
