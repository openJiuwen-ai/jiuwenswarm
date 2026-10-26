# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from jiuwenswarm.extensions.hooks_context import ArtifactPostProcessHookContext
from jiuwenswarm.server.runtime.code_source_unicode import (
    _normalize_code_artifact_hook,
    is_code_unicode_readable_enabled,
    normalize_python_script_file,
    normalize_python_source_unicode_literals,
    register_code_source_unicode_hook,
)


def test_normalize_unicode_escape_to_readable() -> None:
    source = 'doc.add_paragraph("\\u6807\\u9898")\n'
    normalized, count = normalize_python_source_unicode_literals(source)
    assert count == 1
    assert '"标题"' in normalized


def test_raw_string_unchanged() -> None:
    source = 'value = r"\\u6587"\n'
    normalized, count = normalize_python_source_unicode_literals(source)
    assert count == 0
    assert normalized == source


def test_inconsistent_indent_degrades_to_original() -> None:
    """IndentationError from generate_tokens must not propagate."""
    source = 'def f():\n    if True:\n        x = "\\u6807"\n      y = 1\n'
    normalized, count = normalize_python_source_unicode_literals(source)
    assert count == 0
    assert normalized == source


def test_tab_error_degrades_to_original() -> None:
    """TabError from generate_tokens must not propagate (3.11 tokenize may not raise it)."""
    source = 'x = "\\u6807"\n'
    with patch(
        "jiuwenswarm.server.runtime.code_source_unicode.tokenize.generate_tokens",
        side_effect=TabError("inconsistent use of tabs and spaces in indentation"),
    ):
        normalized, count = normalize_python_source_unicode_literals(source)
    assert count == 0
    assert normalized == source


def test_incomplete_token_degrades_to_original() -> None:
    source = 'x = "\\u6807'
    normalized, count = normalize_python_source_unicode_literals(source)
    assert count == 0
    assert normalized == source


def test_surrogate_escape_pair_unchanged() -> None:
    source = 'x = "\\ud83d\\ude00"\n'
    normalized, count = normalize_python_source_unicode_literals(source)
    assert count == 0
    assert normalized == source


def test_normalize_python_script_file_surrogate_preserves_file(tmp_path: Path) -> None:
    source = 'x = "\\ud83d\\ude00"\n'
    script = tmp_path / "demo.py"
    script.write_text(source, encoding="utf-8")
    assert normalize_python_script_file(script) == 0
    assert script.read_text(encoding="utf-8") == source


def test_config_disabled() -> None:
    assert is_code_unicode_readable_enabled({"code_generation": {"unicode_readable": False}}) is False


@pytest.mark.asyncio
async def test_artifact_hook_normalizes_file(tmp_path: Path) -> None:
    script = tmp_path / "demo.py"
    script.write_text('x = "\\u6807\\u9898"\n', encoding="utf-8")
    ctx = ArtifactPostProcessHookContext(
        session_id="s1",
        tool_name="write_file",
        artifact_paths=[str(script)],
    )
    await _normalize_code_artifact_hook(ctx)
    assert script.read_text(encoding="utf-8") == 'x = "标题"\n'


def test_normalize_python_script_file(tmp_path: Path) -> None:
    script = tmp_path / "demo.py"
    script.write_text('x = "\\u6807\\u9898"\n', encoding="utf-8")
    assert normalize_python_script_file(script) == 1
    assert script.read_text(encoding="utf-8") == 'x = "标题"\n'


def test_register_hook_idempotent() -> None:
    registry = MagicMock()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(
            "jiuwenswarm.extensions.registry.ExtensionRegistry.get_instance",
            lambda: registry,
        )
        mp.setattr(
            "jiuwenswarm.server.runtime.code_source_unicode._HOOK_REGISTERED",
            False,
        )
        register_code_source_unicode_hook()
        register_code_source_unicode_hook()
    assert registry.register.call_count == 1
