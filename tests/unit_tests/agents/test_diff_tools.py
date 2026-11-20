# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Unit tests for the zero-dependency diff_files tool."""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from openjiuwen.core.sys_operation.cwd import (
    get_cwd,
    get_project_root,
    get_workspace,
    init_cwd,
)

from jiuwenswarm.agents.harness.common.tools import diff_files as exported_diff_files
from jiuwenswarm.agents.harness.common.tools import diff_tools
from jiuwenswarm.agents.harness.common.tools.diff_tools import (
    DEFAULT_CONTEXT_LINES,
    DEFAULT_MAX_CHARS,
    _RawDiffInputs,
    _normalize_request,
    diff_files,
)


@contextmanager
def _workspace_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Bind a temporary workspace directory as active runtime cwd/project/workspace."""
    prev_cwd = get_cwd()
    prev_project = get_project_root()
    prev_workspace = get_workspace()

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    agent_ws = tmp_path / "agent_ws"
    agent_ws.mkdir()

    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.tools.command_runtime.get_agent_workspace_dir",
        lambda: agent_ws,
    )
    init_cwd(str(workspace), project_root=str(workspace), workspace=str(workspace))
    try:
        yield workspace
    finally:
        init_cwd(prev_cwd, project_root=prev_project, workspace=prev_workspace)


def test_diff_files_card_schema_and_lazy_export() -> None:
    properties = diff_files.card.input_params["properties"]
    assert exported_diff_files is diff_files
    assert diff_files.card.name == "diff_files"
    assert set(properties.keys()) == {
        "file_a",
        "file_b",
        "text_a",
        "text_b",
        "label_a",
        "label_b",
        "context_lines",
        "ignore_trailing_whitespace",
        "ignore_blank_lines",
        "max_chars",
    }
    assert "inputs" not in properties
    for key in properties:
        assert properties[key].get("description")


def test_normalize_request_modes_and_clamping() -> None:
    file_req = _normalize_request(
        _RawDiffInputs(
            file_a="a.py",
            file_b="b.py",
            text_a=None,
            text_b=None,
            label_a=None,
            label_b=None,
            context_lines=None,
            ignore_trailing_whitespace=None,
            ignore_blank_lines=None,
            max_chars=None,
        )
    )
    assert file_req.mode == "file"
    assert file_req.context_lines == DEFAULT_CONTEXT_LINES
    assert file_req.max_chars == DEFAULT_MAX_CHARS

    text_req = _normalize_request(
        _RawDiffInputs(
            file_a=None,
            file_b=None,
            text_a="one\n",
            text_b="two\n",
            label_a="old",
            label_b="new",
            context_lines=99,
            ignore_trailing_whitespace=True,
            ignore_blank_lines=True,
            max_chars=100,
        )
    )
    assert text_req.mode == "text"
    assert text_req.label_a == "old"
    assert text_req.label_b == "new"
    assert text_req.context_lines == 20
    assert text_req.max_chars == 1_000


@pytest.mark.parametrize(
    ("kwargs", "match"),
    [
        ({}, "Must provide either"),
        ({"file_a": "a.py"}, "Both file_a and file_b are required"),
        ({"text_a": "hello"}, "Both text_a and text_b are required"),
        (
            {"file_a": "a.py", "file_b": "b.py", "text_a": "x", "text_b": "y"},
            "not both",
        ),
        ({"file_a": "a\x00.py", "file_b": "b.py"}, "NUL byte"),
        (
            {"text_a": "a", "text_b": "b", "context_lines": True},
            "context_lines must be an integer",
        ),
        (
            {"text_a": "a", "text_b": "b", "ignore_blank_lines": "yes"},
            "ignore_blank_lines must be a boolean",
        ),
    ],
)
def test_normalize_request_validation_errors(
    kwargs: dict[str, object],
    match: str,
) -> None:
    defaults: dict[str, object] = {
        "file_a": None,
        "file_b": None,
        "text_a": None,
        "text_b": None,
        "label_a": None,
        "label_b": None,
        "context_lines": DEFAULT_CONTEXT_LINES,
        "ignore_trailing_whitespace": False,
        "ignore_blank_lines": False,
        "max_chars": DEFAULT_MAX_CHARS,
    }
    defaults.update(kwargs)
    with pytest.raises(ValueError, match=match):
        _normalize_request(_RawDiffInputs(**defaults))


@pytest.mark.asyncio
async def test_diff_files_workspace_files_and_identical(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _workspace_cwd(tmp_path, monkeypatch) as ws:
        file_a = ws / "old.py"
        file_b = ws / "new.py"
        file_a.write_text("alpha = 1\nbeta = 2\ngamma = 3\n", encoding="utf-8")
        file_b.write_text(
            "alpha = 1\nbeta = 20\ngamma = 3\ndelta = 4\n", encoding="utf-8"
        )

        out = await diff_files.invoke({"file_a": "old.py", "file_b": "new.py"})
        assert "Diff Summary: a/old.py -> b/new.py | mode: file" in out
        assert "Stats: +2 insertions, -1 deletions, 1 hunk(s)" in out
        assert "-   2      | beta = 2" in out
        assert "+        2 | beta = 20" in out
        assert "+        4 | delta = 4" in out

        same = await diff_files.invoke({"file_a": "old.py", "file_b": "old.py"})
        assert "No differences found (+0, -0)." in same


@pytest.mark.asyncio
async def test_diff_files_text_mode_and_whitespace_options() -> None:
    text_a = "line1   \n\nline2\n"
    text_b = "line1\nline2\n"

    raw_diff = await diff_files.invoke({"text_a": text_a, "text_b": text_b})
    assert "Stats: +1 insertions, -2 deletions" in raw_diff

    filtered = await diff_files.invoke(
        {
            "text_a": text_a,
            "text_b": text_b,
            "label_a": "before.txt",
            "label_b": "after.txt",
            "ignore_trailing_whitespace": True,
            "ignore_blank_lines": True,
        }
    )
    assert "Diff Summary: a/before.txt -> b/after.txt | mode: text" in filtered
    assert "No differences found (+0, -0)." in filtered


@pytest.mark.asyncio
async def test_diff_files_truncation_at_max_chars() -> None:
    text_a = "\n".join(f"old_line_{i} = {i}" for i in range(200))
    text_b = "\n".join(f"new_line_{i} = {i * 2}" for i in range(200))

    out = await diff_files.invoke(
        {"text_a": text_a, "text_b": text_b, "max_chars": 1_000}
    )
    assert len(out) <= 1_000
    assert "[Truncated at max_chars=1000]" in out


@pytest.mark.asyncio
async def test_diff_files_workspace_security_and_error_handling(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with _workspace_cwd(tmp_path, monkeypatch) as ws:
        inside = ws / "inside.txt"
        inside.write_text("hello\n", encoding="utf-8")
        outside = tmp_path / "outside.txt"
        outside.write_text("secret\n", encoding="utf-8")

        escape_res = await diff_files.invoke(
            {"file_a": "inside.txt", "file_b": "../outside.txt"}
        )
        assert escape_res.startswith("[ERROR]: diff_files failed:")
        assert "outside workspace" in escape_res

        abs_res = await diff_files.invoke(
            {"file_a": "inside.txt", "file_b": str(outside)}
        )
        assert abs_res.startswith("[ERROR]: diff_files failed:")
        assert "outside workspace" in abs_res

        missing_res = await diff_files.invoke(
            {"file_a": "inside.txt", "file_b": "missing.txt"}
        )
        assert missing_res.startswith("[ERROR]: diff_files failed:")
        assert "does not exist" in missing_res

        sub_dir = ws / "subdir"
        sub_dir.mkdir()
        dir_res = await diff_files.invoke({"file_a": "inside.txt", "file_b": "subdir"})
        assert dir_res.startswith("[ERROR]: diff_files failed:")
        assert "directory" in dir_res

        bin_file = ws / "bin.dat"
        bin_file.write_bytes(b"abc\x00def")
        bin_res = await diff_files.invoke({"file_a": "inside.txt", "file_b": "bin.dat"})
        assert bin_res.startswith("[ERROR]: diff_files failed:")
        assert "binary file" in bin_res

        monkeypatch.setattr(diff_tools, "_MAX_FILE_BYTES", 4)
        big_res = await diff_files.invoke(
            {"file_a": "inside.txt", "file_b": "inside.txt"}
        )
        assert big_res.startswith("[ERROR]: diff_files failed:")
        assert "exceeds maximum size" in big_res
