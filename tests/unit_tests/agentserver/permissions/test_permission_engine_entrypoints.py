# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Assert host permission code uses permission_engine official entrypoints."""

from __future__ import annotations

import ast
from pathlib import Path

_REPO = Path(__file__).resolve().parents[4]
_INTERRUPT_HELPERS = (
    _REPO
    / "jiuwenswarm"
    / "agents"
    / "harness"
    / "common"
    / "rails"
    / "interrupt"
    / "interrupt_helpers.py"
)


def test_interrupt_helpers_uses_permission_engine_factory() -> None:
    source = _INTERRUPT_HELPERS.read_text(encoding="utf-8")
    assert "build_permission_interrupt_rail" in source
    assert "from openjiuwen.harness.security.host" not in source
    # A Host subclass is allowed for Smart callbacks; do not mistake its
    # longer class name for direct construction of the SDK base class.
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "PermissionInterruptRail"
        for node in ast.walk(ast.parse(source))
    )


def test_permission_engine_public_factory_import() -> None:
    from openjiuwen.harness.security import (
        PermissionConfirmResponse,
        PermissionEngine,
        ToolPermissionHost,
        build_permission_interrupt_rail,
    )

    assert callable(build_permission_interrupt_rail)
    assert PermissionEngine is not None
    assert ToolPermissionHost is not None
    assert PermissionConfirmResponse.__module__ == (
        "openjiuwen.harness.security.permission_engine.models"
    )
