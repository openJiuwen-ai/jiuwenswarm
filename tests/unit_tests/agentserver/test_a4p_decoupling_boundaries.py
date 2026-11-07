# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Dependency boundaries, including lazy, relative and literal dynamic imports."""

from __future__ import annotations

import ast
from importlib.util import resolve_name
from pathlib import Path

import pytest

import agent_gateway

ROOT = Path(__file__).resolve().parents[3]
PACKAGE = ROOT / "jiuwenswarm"
# gateway 已拆为独立包，边界扫描指向已安装的 agent_gateway 包
GATEWAY_PACKAGE = Path(agent_gateway.__file__).resolve().parent


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
            if node.level:
                # 相对导入做纯字符串解析（包无需真实存在——扫描器检测
                # import 语句本身；越界相对导入在此抛错时回落原始名）
                try:
                    base = resolve_name(name, package)
                except ImportError:
                    base = name
            else:
                base = name
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
    # 扫描锚可为 agent_gateway.*（独立包）或 jiuwenswarm.*
    return name == "a4p" or name.startswith("a4p.") or (
        (
            name.startswith("jiuwenswarm.")
            or name.startswith("agent_gateway.")
            or name.startswith("gateway.")
        )
        and any(part == "a4p" or part.startswith("a4p_") for part in name.split("."))
    )


def _assert_boundary(paths, forbidden, *, root: Path = ROOT, prefix: str = ""):
    paths = sorted(paths)
    assert paths, "Boundary must cover actual implementation files"
    violations = []
    for path in paths:
        relative = path.relative_to(root).with_suffix("")
        module = ".".join(([prefix] if prefix else []) + list(relative.parts))
        for line, target in _imports(path.read_text(encoding="utf-8"), module):
            if forbidden(target):
                violations.append(f"{path.relative_to(root)}:{line}: {target}")
    assert not violations, "Forbidden dependency:\n" + "\n".join(violations)


def test_gateway_does_not_import_a4p_implementation():
    _assert_boundary(
        GATEWAY_PACKAGE.rglob("*.py"),
        _a4p_implementation,
        root=GATEWAY_PACKAGE,
        prefix="agent_gateway",
    )


def test_a4p_implementation_does_not_import_gateway():
    # agent_gateway.common 是无状态纯组件的唯一真源，a4p 允许引用；其余子包禁止。
    _assert_boundary(
        set(PACKAGE.rglob("a4p*.py")) | set((PACKAGE / "agents/harness/common/a4p").rglob("*.py")),
        lambda name: (
            name == "gateway"
            or name.startswith("gateway.")
            or name == "agent_gateway"
            or name.startswith("agent_gateway.")
        )
        and not name.startswith("agent_gateway.common"),
    )


def test_authorizer_broker_does_not_discover_server_singleton():
    path = PACKAGE / "agents/harness/common/a4p/authorizer.py"
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
    "import jiuwenswarm.agents.harness.common.a4p.runtime as runtime",
    "from jiuwenswarm.agents.harness.common.a4p import runtime as a4p_runtime",
    "def lazy():\n from ..agents.harness.common.a4p.runtime import get_a4p_runtime",
    "import importlib as il\nil.import_module('jiuwenswarm.agents.harness.common.a4p.runtime')",
    "from importlib import import_module as load\nload('a4p')",
    "__import__('a4p.types')",
])
def test_import_scanner_detects_boundary_bypasses(source):
    # gateway 是独立包，锚点用其不存在的子包（resolve 仅做字符串拼接，
    # 相对导入 "..agents" 正确解析为越界错误路径）
    assert any(
        _a4p_implementation(name)
        for _, name in _imports(source, "agent_gateway.example.scanner")
    )
