# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Declarative drop-in registry for built-in agent harness tools.

Eliminates merge conflicts across parallel tool PRs by allowing each new tool
module (`*_tools.py` or `specs/*.py`) to declare a top-level
``BUILTIN_TOOL_SPECS`` constant without editing central registry tables.
Discovery parses module ASTs statically so lazy tool imports remain untouched.
"""

from __future__ import annotations

import ast
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

VALID_PERMISSION_SCOPES: frozenset[str] = frozenset(
    {"path_read", "path_write", "public_web_read", "internal_readonly"}
)


@dataclass(frozen=True)
class BuiltinToolSpec:
    """Declarative metadata for a single built-in tool."""

    name: str
    module: str
    symbol: str = ""
    display_en: str = ""
    aliases_zh: tuple[str, ...] = ()
    category_path: str = ""
    category_description: str = ""
    category_description_en: str = ""
    permission_scope: str = "path_read"

    def resolved_symbol(self) -> str:
        """Return the export symbol name, defaulting to ``name``."""
        return self.symbol or self.name


_REGISTERED_SPECS: dict[str, BuiltinToolSpec] = {}
_DISCOVERED_CACHE: dict[Path, tuple[BuiltinToolSpec, ...]] = {}


def register_builtin_tool_spec(spec: BuiltinToolSpec) -> None:
    """Register one ``BuiltinToolSpec`` programmatically.

    Note: this only affects ``discover_builtin_tool_specs``; the import-time snapshot
    tables (lazy exports, discovery metadata, categories, permission sets) are not
    refreshed. It is intended for tests.
    """
    if not spec.name or not spec.module:
        raise ValueError("BuiltinToolSpec requires non-empty name and module")
    if spec.permission_scope not in VALID_PERMISSION_SCOPES:
        raise ValueError(f"Unsupported permission_scope: {spec.permission_scope}")
    _REGISTERED_SPECS[spec.name] = spec
    _DISCOVERED_CACHE.clear()


def clear_registered_tool_specs() -> None:
    """Clear programmatically registered specs and discovery cache (for tests)."""
    _REGISTERED_SPECS.clear()
    _DISCOVERED_CACHE.clear()


def _parse_aliases(raw: Any) -> tuple[str, ...]:
    if isinstance(raw, str):
        return tuple(raw.split())
    if isinstance(raw, (list, tuple)):
        return tuple(str(item).strip() for item in raw if str(item).strip())
    return ()


def _clean_str(val: Any, default: str = "") -> str:
    text = str(val or "").strip()
    return text if text else default


def _spec_from_dict(raw: dict[str, Any], default_module: str) -> BuiltinToolSpec | None:
    name = _clean_str(raw.get("name"))
    if not name:
        return None
    module = _clean_str(raw.get("module"), default_module)
    scope = _clean_str(raw.get("permission_scope"), "path_read")
    if scope not in VALID_PERMISSION_SCOPES:
        logger.warning("[builtin_tool_registry] Invalid permission_scope %r for %s", scope, name)
        return None
    return BuiltinToolSpec(
        name=name,
        module=module,
        symbol=_clean_str(raw.get("symbol"), name),
        display_en=_clean_str(raw.get("display_en")),
        aliases_zh=_parse_aliases(raw.get("aliases_zh")),
        category_path=_clean_str(raw.get("category_path")),
        category_description=_clean_str(raw.get("category_description")),
        category_description_en=_clean_str(raw.get("category_description_en")),
        permission_scope=scope,
    )


_POSITIONAL_FIELDS: tuple[str, ...] = ("name", "module", "symbol")


def _eval_call_keywords(node: ast.Call) -> dict[str, Any] | None:
    """Evaluate a ``BuiltinToolSpec(...)`` call; log a warning on any dropped spec."""
    if not isinstance(node.func, ast.Name) or node.func.id != "BuiltinToolSpec":
        if isinstance(node.func, ast.Attribute) and node.func.attr == "BuiltinToolSpec":
            logger.warning("[builtin_tool_registry] Attribute-form BuiltinToolSpec skipped")
        return None
    if len(node.args) > len(_POSITIONAL_FIELDS):
        logger.warning("[builtin_tool_registry] Too many positional args in spec")
        return None
    pairs = list(zip(_POSITIONAL_FIELDS, node.args))
    pairs += [(kw.arg, kw.value) for kw in node.keywords]
    raw_kw: dict[str, Any] = {}
    for key, value_node in pairs:
        try:
            if key is None:
                raise ValueError("**kwargs unsupported")
            raw_kw[key] = ast.literal_eval(value_node)
        except (ValueError, SyntaxError):
            logger.warning("[builtin_tool_registry] Non-literal spec argument %r", key)
            return None
    return raw_kw


def _eval_spec_node(node: ast.AST, default_module: str) -> BuiltinToolSpec | None:
    if isinstance(node, ast.Dict):
        try:
            raw = ast.literal_eval(node)
        except (ValueError, SyntaxError):
            return None
        return _spec_from_dict(raw, default_module) if isinstance(raw, dict) else None
    if isinstance(node, ast.Call):
        raw_kw = _eval_call_keywords(node)
        return _spec_from_dict(raw_kw, default_module) if raw_kw is not None else None
    return None


def _find_specs_value_node(stmt: ast.stmt) -> ast.AST | None:
    if isinstance(stmt, ast.Assign):
        for target in stmt.targets:
            if isinstance(target, ast.Name) and target.id == "BUILTIN_TOOL_SPECS":
                return stmt.value
    elif isinstance(stmt, ast.AnnAssign):
        if isinstance(stmt.target, ast.Name) and stmt.target.id == "BUILTIN_TOOL_SPECS":
            return stmt.value
    return None


def _collect_specs_from_node(value_node: ast.AST | None, default_module: str) -> list[BuiltinToolSpec]:
    if not isinstance(value_node, (ast.Tuple, ast.List)):
        return []
    found: list[BuiltinToolSpec] = []
    for elt in value_node.elts:
        spec = _eval_spec_node(elt, default_module)
        if spec is not None:
            found.append(spec)
    return found


def _extract_specs_from_file(py_file: Path, default_module: str) -> list[BuiltinToolSpec]:
    try:
        source = py_file.read_text(encoding="utf-8")
        if "BUILTIN_TOOL_SPECS" not in source:
            return []
        tree = ast.parse(source, filename=str(py_file))
    except (OSError, SyntaxError, UnicodeDecodeError):
        return []
    found: list[BuiltinToolSpec] = []
    for stmt in tree.body:
        val_node = _find_specs_value_node(stmt)
        if val_node is not None:
            found.extend(_collect_specs_from_node(val_node, default_module))
    return found


def _candidate_spec_files(base_dir: Path) -> list[tuple[Path, str]]:
    candidates: list[tuple[Path, str]] = []
    specs_dir = base_dir / "specs"
    if specs_dir.is_dir():
        for path in sorted(specs_dir.glob("*.py")):
            if not path.name.startswith("_"):
                candidates.append((path, f".specs.{path.stem}"))
    for path in sorted(base_dir.glob("*_tools.py")):
        candidates.append((path, f".{path.stem}"))
    return candidates


def discover_builtin_tool_specs(tools_dir: Path | None = None) -> tuple[BuiltinToolSpec, ...]:
    """Discover ``BUILTIN_TOOL_SPECS`` across tool modules without importing them."""
    base_dir = (tools_dir or Path(__file__).resolve().parent).resolve()
    cached = _DISCOVERED_CACHE.get(base_dir)
    if cached is not None:
        return cached

    by_name: dict[str, BuiltinToolSpec] = {}
    for py_file, default_mod in _candidate_spec_files(base_dir):
        for spec in _extract_specs_from_file(py_file, default_mod):
            by_name[spec.name] = spec
    if tools_dir is None:
        by_name.update(_REGISTERED_SPECS)

    result = tuple(by_name.values())
    _DISCOVERED_CACHE[base_dir] = result
    return result


def discover_lazy_exports(
    existing: dict[str, tuple[str, str]],
    tools_dir: Path | None = None,
) -> dict[str, tuple[str, str]]:
    """Return ``_EXPORTS`` entries for discovered tools not already in ``existing``."""
    added: dict[str, tuple[str, str]] = {}
    for spec in discover_builtin_tool_specs(tools_dir):
        if spec.name not in existing:
            added[spec.name] = (spec.module, spec.resolved_symbol())
    return added


def _build_new_category(category_cls: type, spec: BuiltinToolSpec) -> Any:
    if not (spec.category_description or spec.display_en):
        logger.warning(
            "[builtin_tool_registry] Category %r declared without description",
            spec.category_path,
        )
    desc_zh = spec.category_description or spec.display_en or spec.category_path
    desc_en = spec.category_description_en or spec.display_en or desc_zh
    return category_cls(
        path=spec.category_path,
        description=desc_zh,
        tools=(spec.name,),
        description_en=desc_en,
    )


def _specs_with_categories(tools_dir: Path | None) -> list[BuiltinToolSpec]:
    return [s for s in discover_builtin_tool_specs(tools_dir) if s.category_path]


def merge_discovered_categories(
    base_categories: tuple[Any, ...],
    category_cls: type,
    tools_dir: Path | None = None,
) -> tuple[Any, ...]:
    """Merge discovered tool category memberships into ``base_categories``."""
    specs = _specs_with_categories(tools_dir)
    if not specs:
        return base_categories

    by_path: dict[str, Any] = {c.path: c for c in base_categories}
    order: list[str] = list(by_path.keys())
    for spec in specs:
        existing = by_path.get(spec.category_path)
        if existing is None:
            by_path[spec.category_path] = _build_new_category(category_cls, spec)
            order.append(spec.category_path)
        elif spec.name not in existing.tools:
            by_path[spec.category_path] = existing._replace(
                tools=(*existing.tools, spec.name),
            )
    return tuple(by_path[p] for p in order)


def discover_permission_sets(
    tools_dir: Path | None = None,
    exclude: frozenset[str] = frozenset(),
) -> dict[str, frozenset[str]]:
    """Group discovered tool names by permission scope for ``tool_capabilities``.

    Names are lower-cased to match ``classify_tool`` lookups. Names in ``exclude``
    (already classified by static sets) keep their existing behavior.
    """
    grouped: dict[str, list[str]] = {
        "path_read": [],
        "path_write": [],
        "public_web_read": [],
        "internal_readonly": [],
    }
    for spec in discover_builtin_tool_specs(tools_dir):
        lowered = spec.name.lower()
        if spec.permission_scope in grouped and lowered not in exclude:
            grouped[spec.permission_scope].append(lowered)
    return {key: frozenset(names) for key, names in grouped.items()}
