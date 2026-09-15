"""Stable facade contracts for task-level automatic permission handling."""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

from openjiuwen.core.single_agent.rail.base import AgentRail

from jiuwenswarm.agents.harness.common.rails.permissions import (
    AutoPermissionInterruptRail as ExportedAutoPermissionInterruptRail,
)
from jiuwenswarm.agents.harness.common.rails.permissions.auto_permission_rail import (
    AutoPermissionInterruptRail,
)


def test_facade_keeps_package_export_and_tool_callbacks(tmp_path: Path) -> None:
    rail = AutoPermissionInterruptRail(
        base_rail=object(),
        permission_config={"mode": "auto"},
        workspace_root=tmp_path,
    )

    assert ExportedAutoPermissionInterruptRail is AutoPermissionInterruptRail
    assert isinstance(rail, AgentRail)
    assert {callback.__name__ for callback in rail.get_callbacks().values()} == {
        "after_tool_call",
        "before_tool_call",
    }


def test_facade_mixins_do_not_shadow_methods() -> None:
    owners: dict[str, list[str]] = {}
    for cls in AutoPermissionInterruptRail.__mro__[1:]:
        if cls is AgentRail:
            break
        for name, value in cls.__dict__.items():
            if inspect.isfunction(value) or isinstance(
                value, (classmethod, staticmethod)
            ):
                owners.setdefault(name, []).append(cls.__name__)

    assert {name: classes for name, classes in owners.items() if len(classes) > 1} == {}


def test_private_package_modules_do_not_import_facade() -> None:
    package_root = Path(
        "jiuwenswarm/agents/harness/common/rails/permissions/_auto_permission"
    )
    for path in package_root.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                assert node.module != (
                    "jiuwenswarm.agents.harness.common.rails.permissions."
                    "auto_permission_rail"
                )
