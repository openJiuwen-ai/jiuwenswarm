# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dependency boundaries, including lazy, relative and literal dynamic imports."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "jiuwenswarm"


def _imports(source: str, module: str):
    tree = ast.parse(source)
    package = module.rpartition(".")[0]
    aliases = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for item in node.names:
                aliases[item.asname or item.name] = item.name
                yield node.lineno, item.name
        elif isinstance(node, ast.ImportFrom):
            name = "." * node.level + (node.module or "")
            base = resolve_name(name, package) if node.level else name
            yield node.lineno, base
            for item in node.names:
                target = f"{base}.{item.name}"
                aliases[item.asname or item.name] = target
                yield node.lineno, target
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = ast.unparse(node.func)
        head, dot, tail = func.partition(".")
        func = aliases.get(head, head) + (dot + tail if dot else "")
        if func not in {"__import__", "importlib.import_module"}:
            continue
        arg = node.args[0]
        if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
            yield node.lineno, arg.value


def _a4p_implementation(name: str) -> bool:
    return name == "a4p" or name.startswith("a4p.") or (
        name.startswith("jiuwenswarm.")
        and any(part.startswith("a4p_") for part in name.split("."))
    )


def _assert_boundary(paths, forbidden):
    paths = sorted(paths)
    assert paths, "Boundary must cover actual implementation files"
    violations = []
    for path in paths:
        module = ".".join(path.relative_to(ROOT).with_suffix("").parts)
        for line, target in _imports(path.read_text(encoding="utf-8"), module):
            if forbidden(target):
                violations.append(f"{path.relative_to(ROOT)}:{line}: {target}")
    assert not violations, "Forbidden dependency:\n" + "\n".join(violations)


def test_gateway_does_not_import_a4p_implementation():
    _assert_boundary((PACKAGE / "gateway").rglob("*.py"), _a4p_implementation)


def test_a4p_implementation_does_not_import_gateway():
    _assert_boundary(
        PACKAGE.rglob("a4p*.py"),
        lambda name: name == "jiuwenswarm.gateway" or name.startswith("jiuwenswarm.gateway."),
    )


def test_authorizer_broker_does_not_discover_server_singleton():
    path = PACKAGE / "agents/harness/common/a4p_authorizer.py"
    _assert_boundary(
        [path],
        lambda name: name == "jiuwenswarm.server" or name.startswith("jiuwenswarm.server."),
    )
    tree = ast.parse(path.read_text(encoding="utf-8"))
    forbidden = {"get_agent_ws_server", "get_agent_websocket_server", "AgentWebSocketServer"}
    references = {
        node.id if isinstance(node, ast.Name) else node.attr
        for node in ast.walk(tree)
        if isinstance(node, (ast.Name, ast.Attribute))
    }
    assert not references & forbidden


@pytest.mark.parametrize("source", [
    "import jiuwenswarm.agents.harness.common.a4p_runtime as runtime",
    "from jiuwenswarm.agents.harness.common import a4p_runtime",
    "def lazy():\n from ..agents.harness.common.a4p_runtime import get_a4p_runtime",
    "import importlib as il\nil.import_module('jiuwenswarm.agents.harness.common.a4p_runtime')",
    "from importlib import import_module as load\nload('a4p')",
    "__import__('a4p.types')",
])
def test_import_scanner_detects_boundary_bypasses(source):
    assert any(_a4p_implementation(name) for _, name in _imports(source, "jiuwenswarm.gateway.example"))
