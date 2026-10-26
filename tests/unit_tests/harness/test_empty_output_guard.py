# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Regression tests for the empty-output / bash-env hardening changes.

Covers four new pieces of logic introduced by the
``fix/office-empty-output-guard-ee`` branch:

* :func:`jiuwenswarm.agents.harness.flash.tools.flash_read_tool.
  FlashReadFileTool._annotate_empty_xlsx` — empty-xlsx banner.
* :func:`jiuwenswarm.agents.harness.common.tools.send_file_to_user.
  _xlsx_size_warning` / :func:`_append_size_warnings` — small-xlsx hint.
* :func:`jiuwenswarm.agents.harness.common.tools.command_tools.
  _discover_windows_tool_dirs` and :func:`_prepend_windows_tool_paths` —
  trusted-vs-workspace PATH split (security boundary).
* :func:`jiuwenswarm.agents.harness.common.tools.command_tools.
  _windows_not_found_hint` — diagnostic hint trigger conditions.

These tests run cross-platform; on non-Windows hosts the PATH-hardening
tests assert the discovery split and the dedup/no-op semantics without
depending on real OfficeAce installs.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from jiuwenswarm.agents.harness.common.tools import command_tools as ct
from jiuwenswarm.agents.harness.common.tools import send_file_to_user as sfu
from jiuwenswarm.agents.harness.flash.tools.flash_read_tool import (
    FlashReadFileTool,
)


# ---------------------------------------------------------------------------
# 1) _annotate_empty_xlsx banner detection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "rendered, expect_banner",
    [
        # Exactly one sheet header + zero data rows → banner injected.
        (
            "## Sheet1\n",
            True,
        ),
        # Line-number prefix must be stripped before counting.
        (
            "     1\t## Sheet1\n",
            True,
        ),
        # Data rows present → no banner.
        (
            "## Sheet1\n"
            "     2\t| col |\n"
            "     3\t| --- |\n"
            "     4\t| a   |\n",
            False,
        ),
        # Multiple sheet headers → not "only default sheet", no banner.
        (
            "## Sheet1\n## Sheet2\n",
            False,
        ),
        # Empty content → no banner.
        ("", False),
        # Whitespace only → no banner.
        ("   \n\n", False),
        # Dict form: same rules apply, content key is annotated.
        (
            {"content": "## Sheet1\n", "line_count": 1},
            True,
        ),
        (
            {"content": "## Sheet1\n| a |\n", "line_count": 2},
            False,
        ),
    ],
)
def test_annotate_empty_xlsx_banner(
    rendered: Any, expect_banner: bool
) -> None:
    result = FlashReadFileTool._annotate_empty_xlsx(rendered)
    if expect_banner:
        if isinstance(rendered, dict):
            assert isinstance(result, dict)
            content = result["content"]
        else:
            content = result
        assert "⚠️" in content
        assert "该工作簿内容为空" in content
    else:
        if isinstance(rendered, dict):
            assert result == rendered or result["content"] == rendered["content"]
        else:
            assert result == rendered


def test_annotate_single_xlsx_skips_non_xlsx() -> None:
    """Single-file annotation is gated by .xlsx suffix only."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    tool = FlashReadFileTool.__new__(FlashReadFileTool)
    result = ToolOutput(success=True, data={"content": "## Sheet1\n"})
    out = tool._annotate_single_xlsx({"file_path": "/tmp/note.txt"}, result)
    assert out is result


def test_annotate_single_xlsx_passes_through_when_not_dict() -> None:
    """Non-dict data shapes (multimodal streams) are left untouched."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    tool = FlashReadFileTool.__new__(FlashReadFileTool)
    result = ToolOutput(success=True, data=["not", "a", "dict"])
    out = tool._annotate_single_xlsx({"file_path": "/tmp/book.xlsx"}, result)
    assert out is result


# ---------------------------------------------------------------------------
# 2) _xlsx_size_warning / _append_size_warnings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "size, ext, expect_warning",
    [
        (5_500, ".xlsx", True),
        (5_999, ".xlsm", True),
        (6_000, ".xlsx", False),
        (10_000, ".xlsx", False),
        (1_000, ".csv", False),
        (1_000, ".docx", False),
    ],
)
def test_xlsx_size_warning_thresholds(
    tmp_path: Path, size: int, ext: str, expect_warning: bool
) -> None:
    path = tmp_path / f"book{ext}"
    path.write_bytes(b"\x00" * size)
    warning = sfu._xlsx_size_warning(str(path))
    if expect_warning:
        assert warning is not None
        assert "⚠️" in warning
        assert str(size) in warning
    else:
        assert warning is None


def test_xlsx_size_warning_returns_none_on_missing_file() -> None:
    assert sfu._xlsx_size_warning("/nonexistent/never/created.xlsx") is None


def test_append_size_warnings_appends_each() -> None:
    result = sfu._append_size_warnings(
        "base result",
        ["warning 1", "warning 2"],
    )
    assert result.startswith("base result\n")
    assert "warning 1" in result
    assert "warning 2" in result


def test_append_size_warnings_no_op_when_empty() -> None:
    assert sfu._append_size_warnings("base", []) == "base"
    # Implementation contract: empty result stays empty (we never inject
    # warnings without a base to anchor them).
    assert sfu._append_size_warnings("", ["warn"]) == ""


# ---------------------------------------------------------------------------
# 3) _discover_windows_tool_dirs / _prepend_windows_tool_paths
# ---------------------------------------------------------------------------


def _fresh_discover() -> tuple[list[str], list[str]]:
    """Reset the cached discovery so each test exercises discovery afresh.

    Kept for callers that prefer an explicit reset; the
    ``_reset_windows_tool_dirs_cache`` autouse fixture already invalidates
    the cache between tests.
    """
    ct._WINDOWS_TOOL_DIRS = None
    return ct._discover_windows_tool_dirs()


@pytest.fixture
def fake_officeace_root(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Plant a fake OfficeAce install under Program Files."""
    prog = tmp_path / "Program Files"
    officeace = prog / "OfficeAce"
    bin_dir = officeace / "office-claw-skills" / "common_binary" / "windows"
    bin_dir.mkdir(parents=True)
    (officeace / "tools" / "python").mkdir(parents=True)
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(prog))
    return officeace


@pytest.fixture
def readonly_dir(tmp_path: Path) -> Path:
    """Create a sub-tree and mark it read-only for the current user.

    :func:`os.access(..., os.W_OK)` returns ``False`` once read-only perms
    are set; this is the same check :func:`_discover_windows_tool_dirs`
    uses to gate workspace-relative PATH additions.
    """
    readonly_root = tmp_path / "readonly_workspace"
    readonly_root.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(readonly_root, 0o555)
    except (PermissionError, OSError):
        pass
    return readonly_root


@pytest.fixture
def writable_workspace_skill(tmp_path: Path) -> Path:
    """Create a user-writable office-claw-skills subtree to test poisoning."""
    workspace_skill = (
        tmp_path / "office-claw-skills" / "common_binary" / "windows"
    )
    workspace_skill.mkdir(parents=True)
    return workspace_skill


@pytest.fixture(autouse=True)
def _reset_windows_tool_dirs_cache() -> None:
    """Each test starts with a clean discovery cache."""
    ct._WINDOWS_TOOL_DIRS = None
    yield
    ct._WINDOWS_TOOL_DIRS = None


def test_discover_trusted_dirs_came_from_officeace_root(
    fake_officeace_root: Path,
) -> None:
    trusted, _ = ct._discover_windows_tool_dirs()
    assert any(str(fake_officeace_root) in d for d in trusted), trusted


def _force_readonly(path: Path) -> None:
    """Patch :func:`os.access` to claim *path* is read-only.

    On Windows even ``chmod 0o555`` does not always flip
    :func:`os.access(..., os.W_OK)` to ``False`` (administrators bypass ACLs);
    a targeted ``os.access`` mock gives the test a deterministic answer.
    """
    real_access = os.access

    def _patched(p: Any, mode: int) -> bool:
        try:
            if Path(p).resolve() == Path(path).resolve():
                if mode == os.W_OK:
                    return False
                if mode == os.R_OK:
                    return True
        except (OSError, ValueError):
            pass
        return real_access(p, mode)

    return _patched


def test_discover_workspace_dirs_are_bounded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace hit at depth 4 picked up; depth 6 (out of boundary) ignored."""
    chain = [tmp_path]
    for _ in range(6):
        chain.append(chain[-1] / "child")
        chain[-1].mkdir()
    target = chain[4] / "office-claw-skills" / "common_binary" / "windows"
    target.mkdir(parents=True)
    try:
        os.chmod(target, 0o555)
    except (PermissionError, OSError):
        pass
    monkeypatch.setattr(
        "os.access", _force_readonly(target)
    )
    with patch("os.getcwd", return_value=str(chain[-1])):
        _, workspace = ct._discover_windows_tool_dirs()
    assert any("child" in d for d in workspace), workspace
    assert not any(str(chain[6]) in d for d in workspace), workspace


def test_discover_skips_user_writable_workspace_dir(
    writable_workspace_skill: Path,
) -> None:
    """Workspace hit that the current user can write to must be skipped."""
    with patch("os.getcwd", return_value=str(writable_workspace_skill.parent.parent)):
        _, workspace = ct._discover_windows_tool_dirs()
    assert str(writable_workspace_skill) not in workspace, workspace


def test_prepend_does_not_prepend_workspace_paths(
    fake_officeace_root: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Workspace hits must be appended, not prepended, ahead of system PATH."""
    workspace_skill = (
        fake_officeace_root.parent / "readonly_workspace"
        / "office-claw-skills" / "common_binary" / "windows"
    )
    workspace_skill.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(workspace_skill, 0o555)
    except (PermissionError, OSError):
        pass
    monkeypatch.setattr(
        "os.access", _force_readonly(workspace_skill)
    )
    # System PATH tokens must contain no os.pathsep (";" / ":") so this test
    # runs on both Windows and POSIX (where os.pathsep is ":", splitting
    # "C:\\system1" into ["C", "\\system1"]).
    system_dirs = ["sentinel1", "sentinel2"]
    initial_path = os.pathsep.join(system_dirs)
    with patch("os.getcwd", return_value=str(workspace_skill.parent.parent.parent)):
        env = {"PATH": initial_path}
        ct._prepend_windows_tool_paths(env)
    parts = env["PATH"].split(os.pathsep)
    trusted_indices = [
        i for i, p in enumerate(parts) if str(fake_officeace_root) in p
    ]
    assert trusted_indices, parts
    for d in system_dirs:
        assert d in parts
    workspace_indices = [
        i for i, p in enumerate(parts) if str(workspace_skill) in p
    ]
    assert workspace_indices, parts
    system_max_idx = max(parts.index(d) for d in system_dirs)
    assert min(workspace_indices) > system_max_idx, parts
    assert min(trusted_indices) < system_max_idx, parts


def test_prepend_drops_user_writable_workspace_entirely(
    writable_workspace_skill: Path,
) -> None:
    """User-writable workspace hits must not appear in the merged PATH at all."""
    with patch("os.getcwd", return_value=str(writable_workspace_skill.parent.parent)):
        env = {"PATH": "C:\\system1"}
        ct._prepend_windows_tool_paths(env)
    parts = env["PATH"].split(os.pathsep)
    assert str(writable_workspace_skill) not in parts, parts


def test_prepend_is_deduped(fake_officeace_root: Path) -> None:
    """Calling the helper twice must not duplicate entries."""
    with patch.dict(os.environ, {}, clear=False):
        env = {"PATH": "/usr/bin"}
        ct._WINDOWS_TOOL_DIRS = None
        ct._prepend_windows_tool_paths(env)
        first = env["PATH"]
        ct._prepend_windows_tool_paths(env)
        second = env["PATH"]
    assert first == second


def test_prepend_is_noop_without_tool_dirs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If no trusted or workspace dirs exist, PATH is left untouched."""
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    monkeypatch.setenv("ProgramFiles", str(tmp_path / "Program Files"))
    monkeypatch.delenv("ProgramFiles", raising=False)
    with patch("os.getcwd", return_value=str(tmp_path)):
        ct._WINDOWS_TOOL_DIRS = None
        env = {"PATH": "/usr/bin"}
        ct._prepend_windows_tool_paths(env)
    assert env["PATH"] == "/usr/bin"


# ---------------------------------------------------------------------------
# 4) _windows_not_found_hint
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PATH hardening")
@pytest.mark.parametrize(
    "stderr, exit_code, expect_hint",
    [
        ("python : The term 'python' is not recognized", 1, True),
        ("'python' 不是内部或外部命令", 1, True),
        ("python : 不是可识别的 cmdlet", 1, True),
        ("python : 'python' is not recognized as an internal or external command", 1, True),
        ("some unrelated stderr", 1, False),
        ("python not recognized", 9009, True),
        ("", 0, False),
        # Empty command name disables the hint even when stderr matches.
        ("", 1, False),
        ("not recognized", 1, True),  # stderr matches → hint emitted
    ],
)
def test_windows_not_found_hint_triggers(
    stderr: str, exit_code: int, expect_hint: bool
) -> None:
    command = "" if not stderr else "python gen.py"
    hint = ct._windows_not_found_hint(command, stderr, exit_code)
    if expect_hint:
        assert hint is not None
        assert "[Diagnostic]" in hint
        assert "python" in hint
    else:
        assert hint is None


def test_windows_not_found_hint_skipped_on_non_windows() -> None:
    """The hint is Windows-only even if stderr matches the markers."""
    if os.name == "nt":
        pytest.skip("windows-only invariant")
    assert (
        ct._windows_not_found_hint(
            "python gen.py",
            "python not recognized",
            1,
        )
        is None
    )


# ---------------------------------------------------------------------------
# 5) _build_filesystem_rail fallback on flash import error
# ---------------------------------------------------------------------------


def test_deep_filesystem_rail_returns_flash_when_available() -> None:
    """When the flash package is present, the flash rail is returned."""
    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep

    rail = deep.JiuWenSwarmDeepAdapter._build_filesystem_rail()
    assert rail is not None
    assert type(rail).__name__ == "FlashBashSysOperationRail"


def test_deep_filesystem_rail_falls_back_to_stock_on_import_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When flash_bash_tool raises ImportError, _build_filesystem_rail
    returns a stock ``SysOperationRail`` instead of ``None``."""
    import importlib
    import sys

    from jiuwenswarm.server.runtime.agent_adapter import interface_deep as deep

    real_module = sys.modules.get(
        "jiuwenswarm.agents.harness.flash.tools.flash_bash_tool"
    )
    sys.modules[
        "jiuwenswarm.agents.harness.flash.tools.flash_bash_tool"
    ] = None  # type: ignore[assignment]
    try:
        # Force a fresh import to take the new sys.modules entry.
        importlib.invalidate_caches()
        rail = deep.JiuWenSwarmDeepAdapter._build_filesystem_rail()
    finally:
        if real_module is not None:
            sys.modules[
                "jiuwenswarm.agents.harness.flash.tools.flash_bash_tool"
            ] = real_module
        else:
            sys.modules.pop(
                "jiuwenswarm.agents.harness.flash.tools.flash_bash_tool",
                None,
            )
        importlib.invalidate_caches()
    assert rail is not None
    assert type(rail).__name__ == "SysOperationRail"


# ---------------------------------------------------------------------------
# 6) FlashBashTool.stream hardening parity
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_flash_bash_tool_stream_hardens_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Stream path must apply PATH hardening just like invoke does."""
    from typing import AsyncIterator, Dict

    from openjiuwen.harness.tools.base_tool import ToolOutput
    from openjiuwen.harness.tools.shell.bash._tool import BashTool

    captured: dict = {}

    async def _fake_stream(self, inputs: Dict[str, Any], **kwargs: Any) -> AsyncIterator[ToolOutput]:
        captured["inputs"] = inputs
        yield ToolOutput(
            success=False,
            data={"text": "python not recognized"},
            error=None,
        )
        yield ToolOutput(
            success=False,
            data={"content": "python not recognized"},
            error="python not recognized",
        )

    monkeypatch.setattr(BashTool, "stream", _fake_stream)

    from unittest.mock import MagicMock

    from jiuwenswarm.agents.harness.flash.tools.flash_bash_tool import (
        FlashBashTool,
    )

    tool = FlashBashTool(MagicMock(), language="en", agent_id="test")
    outputs: list[ToolOutput] = []
    async for chunk in tool.stream({"command": "python gen.py"}):
        outputs.append(chunk)

    if os.name == "nt":
        # Hardened environment was injected.
        assert "environment" in captured["inputs"]
    # Last chunk contains the diagnostic hint (Windows only).
    final = outputs[-1]
    assert isinstance(final.data, dict)
    if os.name == "nt":
        assert "[Diagnostic]" in (final.data.get("content") or "")


# ---------------------------------------------------------------------------
# 7) FlashBashTool._hardened_inputs — explicit env priority + idempotence
# ---------------------------------------------------------------------------


@pytest.fixture
def _flash_bash_tool():
    from unittest.mock import MagicMock

    from jiuwenswarm.agents.harness.flash.tools.flash_bash_tool import (
        FlashBashTool,
    )

    return FlashBashTool(MagicMock(), language="en", agent_id="test")


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hardening behavior")
def test_hardened_inputs_injects_when_no_env(
    _flash_bash_tool,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without explicit environment/env, a hardened env is injected."""
    inputs = {"command": "python gen.py"}
    out = _flash_bash_tool._hardened_inputs(inputs)
    assert "environment" in out
    assert "PATH" in out["environment"]


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hardening behavior")
def test_hardened_inputs_does_not_overwrite_explicit_environment(
    _flash_bash_tool,
) -> None:
    """Caller-supplied ``environment`` must win — never overwrite."""
    sentinel_env = {"PATH": "/sentinel/bin", "CUSTOM": "1"}
    inputs = {"command": "python gen.py", "environment": sentinel_env}
    out = _flash_bash_tool._hardened_inputs(inputs)
    assert out["environment"]["PATH"] == "/sentinel/bin"
    assert out["environment"]["CUSTOM"] == "1"


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hardening behavior")
def test_hardened_inputs_does_not_overwrite_explicit_env_alias(
    _flash_bash_tool,
) -> None:
    """``env`` alias must also be honored — caller-supplied env wins.

    The caller supplies ``env`` (the alias) without ``environment``; we must
    not silently overwrite the alias by writing a new ``environment`` key.
    """
    sentinel_env = {"PATH": "/sentinel-alias/bin"}
    inputs = {"command": "python gen.py", "env": sentinel_env}
    out = _flash_bash_tool._hardened_inputs(inputs)
    assert out.get("environment") is None
    assert out.get("env") == sentinel_env


@pytest.mark.skipif(os.name != "nt", reason="Windows-only hardening behavior")
def test_hardened_inputs_returns_independent_copy(
    _flash_bash_tool,
) -> None:
    """The hardened copy must not mutate the caller's inputs dict."""
    inputs = {"command": "python gen.py"}
    out = _flash_bash_tool._hardened_inputs(inputs)
    assert out is not inputs
    assert "environment" not in inputs


def test_hardened_inputs_is_noop_on_non_windows(_flash_bash_tool) -> None:
    """POSIX is left untouched (PATH is managed by the shell / service)."""
    if os.name == "nt":
        pytest.skip("windows-only invariant")
    inputs = {"command": "python gen.py"}
    out = _flash_bash_tool._hardened_inputs(inputs)
    assert out is inputs


# ---------------------------------------------------------------------------
# 8) _annotate_empty_xlsx — extended coverage
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "content, expect_banner",
    [
        # Multi-sheet workbook with no data → no banner (not "only default").
        ("## Sheet1\n## Sheet2\n## Sheet3\n", False),
        # Data table has at least one markdown row → no banner.
        ("## Sheet1\n| a | b |\n|---|---|\n| 1 | 2 |\n", False),
        # Plain text with no sheet headers / table rows → triggers banner
        # (no data rows, header_count <= 1).
        ("Just plain text\nwith lines\n", True),
        # Whitespace-only line-number prefix → triggers banner (no rows).
        ("     1\t\n     2\t\n", True),
        # Line number prefix + single sheet → triggers banner.
        ("     1\t## Sheet1\n     2\t\n", True),
    ],
)
def test_annotate_empty_xlsx_extended(content: str, expect_banner: bool) -> None:
    """Extended coverage: multi-sheet, plain text, whitespace, prefix."""
    rendered = {"content": content}
    out = FlashReadFileTool._annotate_empty_xlsx(rendered)
    assert isinstance(out, dict)
    if expect_banner:
        assert "该工作簿内容为空" in out["content"], out["content"]
    else:
        assert "该工作簿内容为空" not in out["content"], out["content"]


# ---------------------------------------------------------------------------
# 9) _annotate_single_xlsx — multi-sheet + line-prefix + non-xlsx pass-through
# ---------------------------------------------------------------------------


def test_annotate_single_xlsx_annotates_only_empty_default_sheet() -> None:
    """Single-file path with one default Sheet must trigger banner."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    tool = FlashReadFileTool.__new__(FlashReadFileTool)
    result = ToolOutput(
        success=True,
        data={"content": "## Sheet1\n"},
        error=None,
    )
    out = tool._annotate_single_xlsx({"file_path": "/tmp/book.xlsx"}, result)
    assert out is not result
    assert "该工作簿内容为空" in out.data["content"]


def test_annotate_single_xlsx_skips_multi_sheet_even_with_empty_data() -> None:
    """Single-file path with multiple sheets is *not* a default-empty workbook."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    tool = FlashReadFileTool.__new__(FlashReadFileTool)
    result = ToolOutput(
        success=True,
        data={"content": "## Sheet1\n## Sheet2\n"},
        error=None,
    )
    out = tool._annotate_single_xlsx({"file_path": "/tmp/book.xlsx"}, result)
    assert out is result


# ---------------------------------------------------------------------------
# 10) _xlsx_size_warning — 6000-byte boundary + .xls not warned
# ---------------------------------------------------------------------------


def test_xlsx_size_warning_excludes_xls(tmp_path: Path) -> None:
    """Legacy ``.xls`` (binary) is not a target of the heuristic."""
    p = tmp_path / "old.xls"
    p.write_bytes(b"\x00" * 1000)
    assert sfu._xlsx_size_warning(str(p)) is None


def test_xlsx_size_warning_5999_vs_6000_boundary(tmp_path: Path) -> None:
    """Boundary: 5999 → warn, 6000 → no warn."""
    below = tmp_path / "below.xlsx"
    below.write_bytes(b"\x00" * 5999)
    at_or_above = tmp_path / "above.xlsx"
    at_or_above.write_bytes(b"\x00" * 6000)
    assert sfu._xlsx_size_warning(str(below)) is not None
    assert sfu._xlsx_size_warning(str(at_or_above)) is None


# ---------------------------------------------------------------------------
# 11) _windows_not_found_hint — marker hits + exit_code 9009
# ---------------------------------------------------------------------------


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PATH hardening")
def test_windows_not_found_hint_marker_text_paths() -> None:
    """All three marker families trigger the hint."""
    assert (
        ct._windows_not_found_hint("python x.py", "python is not recognized", 1)
        is not None
    )
    assert (
        ct._windows_not_found_hint("python x.py", "'python' 不是内部或外部命令", 1)
        is not None
    )
    assert (
        ct._windows_not_found_hint("python x.py", "python 不是可识别的 cmdlet", 1)
        is not None
    )


@pytest.mark.skipif(os.name != "nt", reason="Windows-only PATH hardening")
def test_windows_not_found_hint_exit_code_9009_path() -> None:
    """exit_code=9009 path triggers regardless of stderr text."""
    assert (
        ct._windows_not_found_hint("python x.py", "anything", 9009)
        is not None
    )


def test_windows_not_found_hint_unrelated_stderr_no_hint() -> None:
    """Neither a marker nor 9009 → no hint."""
    assert (
        ct._windows_not_found_hint("python x.py", "OK", 0) is None
    )


# ---------------------------------------------------------------------------
# 12) _path_key — case-insensitive PATH lookup
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "key",
    ["PATH", "Path", "path"],
)
def test_path_key_finds_existing_canonical_key(key: str) -> None:
    """All three case variants resolve to the actual key in the dict."""
    env = {key: "C:\\System32", "OTHER": "1"}
    assert ct._path_key(env) == key


def test_path_key_falls_back_to_canonical_when_missing() -> None:
    """No PATH-ish key present → default canonical "PATH"."""
    env = {"WINDIR": "C:\\Windows"}
    assert ct._path_key(env) == "PATH"


def test_prepend_windows_tool_paths_preserves_mixed_case_key() -> None:
    """Caller-supplied ``Path`` (mixed case) must be written back under
    the same key, with the original system PATH preserved and no duplicate
    ``PATH`` key emitted."""
    ct._WINDOWS_TOOL_DIRS = (["C:\\TrustedTool"], [])
    env = {"Path": "C:\\System32"}
    ct._prepend_windows_tool_paths(env)
    assert "PATH" not in env, env
    assert "Path" in env, env
    assert env["Path"].startswith("C:\\TrustedTool" + os.pathsep), env
    assert "C:\\System32" in env["Path"], env


def test_prepend_windows_tool_paths_uses_lowercase_key() -> None:
    """Lowercase ``path`` key is honored the same way."""
    ct._WINDOWS_TOOL_DIRS = (["C:\\TrustedTool"], [])
    env = {"path": "C:\\System32"}
    ct._prepend_windows_tool_paths(env)
    assert "PATH" not in env
    assert "path" in env
    assert "C:\\System32" in env["path"]


# ---------------------------------------------------------------------------
# 13) _discover_windows_tool_dirs — empty env vars + cwd OSError
# ---------------------------------------------------------------------------


def test_discover_with_empty_env_vars_returns_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Empty LOCALAPPDATA / ProgramFiles must yield no relative trusted paths."""
    monkeypatch.setenv("LOCALAPPDATA", "")
    monkeypatch.setenv("ProgramFiles", "")
    with patch("os.getcwd", return_value=str(tmp_path)):
        trusted, workspace = ct._discover_windows_tool_dirs()
    for d in trusted:
        assert os.path.isabs(d), d
    assert workspace == []


def test_discover_tolerates_cwd_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """cwd deleted/unavailable → skip the workspace walk, return safely."""
    def _raise_getcwd():
        raise OSError("no current working directory")

    monkeypatch.setattr("os.getcwd", _raise_getcwd)
    trusted, workspace = ct._discover_windows_tool_dirs()
    assert workspace == []
    # trusted may still pick up real OfficeAce install; just assert no exception
    assert isinstance(trusted, list)


def test_discover_tolerates_local_app_data_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """LOCALAPPDATA / ProgramFiles unset (None) must not yield relative paths."""
    monkeypatch.delenv("LOCALAPPDATA", raising=False)
    monkeypatch.delenv("ProgramFiles", raising=False)
    with patch("os.getcwd", return_value="C:\\anywhere"):
        trusted, workspace = ct._discover_windows_tool_dirs()
    for d in trusted:
        assert os.path.isabs(d), d


# ---------------------------------------------------------------------------
# 14) stderr truncation must preserve hint
# ---------------------------------------------------------------------------


def test_stderr_truncation_preserves_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Long stderr + hint: hint must survive truncation, not be sliced off."""
    from jiuwenswarm.agents.harness.common.tools import command_tools as ct2

    captured: dict = {}

    def _fake_clip(value: str, max_chars: int) -> str:
        captured["max_chars"] = max_chars
        captured["input_len"] = len(value)
        if max_chars <= 0 or len(value) <= max_chars:
            return value
        return value[:max_chars] + "\n...[truncated]"

    monkeypatch.setattr(ct2, "_clip_text", _fake_clip)

    long_stderr = "x" * 5000
    monkeypatch.setattr(
        ct2,
        "_windows_not_found_hint",
        lambda *_a, **_k: "\n[Diagnostic] python not found",
    )

    hint = ct2._windows_not_found_hint("python", long_stderr, 1)
    max_output_chars = 2000
    hint_text = hint or ""
    base_budget = max(0, max_output_chars - len(hint_text))
    stderr_text = ct2._clip_text(long_stderr, base_budget) + hint_text
    # _clip_text was called with reduced budget, not the full max_output_chars.
    assert captured["max_chars"] == max_output_chars - len(hint_text)
    # Final stderr ends with the hint.
    assert stderr_text.endswith("[Diagnostic] python not found"), stderr_text[-50:]
    # Final stderr does not end with the truncation marker (hint pushed it off).
    assert not stderr_text.endswith("...[truncated]"), stderr_text[-50:]


def test_stderr_truncation_no_hint_uses_full_budget(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No hint → full max_output_chars budget goes to stderr."""
    from jiuwenswarm.agents.harness.common.tools import command_tools as ct2

    captured: dict = {}

    def _fake_clip(value: str, max_chars: int) -> str:
        captured["max_chars"] = max_chars
        return value if max_chars <= 0 or len(value) <= max_chars else value[:max_chars]

    monkeypatch.setattr(ct2, "_clip_text", _fake_clip)

    long_stderr = "x" * 5000
    hint = None  # no hint
    max_output_chars = 2000
    hint_text = hint or ""
    base_budget = max(0, max_output_chars - len(hint_text))
    _ = ct2._clip_text(long_stderr, base_budget) + hint_text
    assert captured["max_chars"] == max_output_chars


# ---------------------------------------------------------------------------
# 15) _annotate_single_xlsx must not pollute error on success
# ---------------------------------------------------------------------------


def test_annotate_single_xlsx_preserves_none_error_on_success() -> None:
    """A successful empty-xlsx read must NOT stuff the banner into error."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    tool = FlashReadFileTool.__new__(FlashReadFileTool)
    result = ToolOutput(
        success=True,
        data={"content": "## Sheet1\n"},
        error=None,
    )
    out = tool._annotate_single_xlsx({"file_path": "/tmp/book.xlsx"}, result)
    assert out.error is None, f"error must remain None, got {out.error!r}"


def test_annotate_single_xlsx_preserves_real_error_on_failure() -> None:
    """A real error string must survive the annotation untouched."""
    from openjiuwen.harness.tools.base_tool import ToolOutput

    tool = FlashReadFileTool.__new__(FlashReadFileTool)
    sentinel_error = "EACCES: permission denied"
    result = ToolOutput(
        success=False,
        data={"content": "## Sheet1\n"},
        error=sentinel_error,
    )
    out = tool._annotate_single_xlsx({"file_path": "/tmp/book.xlsx"}, result)
    assert out.error == sentinel_error, out.error