from __future__ import annotations

import ast
from pathlib import Path

import pytest


ROOT = Path(__file__).parents[3]
TARGETS = (
    (
        ROOT / "jiuwenswarm/server/personal_context/ws_handler.py",
        "_notify_runtime_enabled",
    ),
    (
        ROOT / "jiuwenswarm/server/runtime/agent_manager.py",
        "set_personal_context_runtime_enabled",
    ),
    (
        ROOT / "jiuwenswarm/server/runtime/agent_adapter/interface_deep.py",
        "refresh_personal_context_rail",
    ),
)


@pytest.mark.parametrize(
    ("path", "function_name"),
    TARGETS,
    ids=[function_name for _, function_name in TARGETS],
)
def test_cancelled_error_is_not_raised_inside_except_handler(
    path: Path,
    function_name: str,
) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    functions = [
        node
        for node in ast.walk(tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == function_name
    ]
    assert len(functions) == 1, (path, function_name)

    handlers = [
        node
        for node in ast.walk(functions[0])
        if isinstance(node, ast.ExceptHandler)
        and isinstance(node.type, ast.Attribute)
        and isinstance(node.type.value, ast.Name)
        and node.type.value.id == "asyncio"
        and node.type.attr == "CancelledError"
    ]

    assert handlers, (path, function_name)
    assert all(
        not any(isinstance(statement, ast.Raise) for statement in handler.body)
        for handler in handlers
    ), (path, function_name)
