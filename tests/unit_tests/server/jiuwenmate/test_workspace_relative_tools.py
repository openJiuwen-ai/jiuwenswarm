"""Colleague write tools describe and accept office-relative paths."""

from __future__ import annotations

import json

import pytest

from openjiuwen.harness.persistent_agent.workspace.file_access import WorkspaceWriteAdapter
from openjiuwen.harness.tools.filesystem import EditFileTool, WriteFileTool

from jiuwenswarm.server.jiuwenmate.workspace_relative_tools import (
    install_office_relative_write_tools,
    office_relative_file_path,
    rewrite_office_absolute_argument,
    rewrite_tool_args,
)


@pytest.fixture(scope="module", autouse=True)
def _installed_office_relative_tools():
    install_office_relative_write_tools()
    install_office_relative_write_tools()


def _path_description(card) -> str:
    return card.input_params["properties"]["file_path"]["description"]


def test_write_and_edit_cards_ask_for_an_office_relative_path():
    write_tool = WriteFileTool(object(), "cn")
    edit_tool = EditFileTool(object(), "cn")
    original_write = _path_description(write_tool.card)
    original_edit = _path_description(edit_tool.card)

    adapter_write = WorkspaceWriteAdapter(write_tool)
    adapter_edit = WorkspaceWriteAdapter(edit_tool)

    assert "新建文件默认写在办公目录根下" in _path_description(adapter_write.card)
    assert "要写入的文件绝对路径" not in _path_description(adapter_write.card)
    assert "办公目录内要修改的文件的相对路径" in _path_description(adapter_edit.card)
    assert "要编辑的文件绝对路径" not in _path_description(adapter_edit.card)
    assert _path_description(write_tool.card) == original_write
    assert _path_description(edit_tool.card) == original_edit


def test_english_write_card_no_longer_requires_an_absolute_path():
    tool = WriteFileTool(object(), "en")
    adapter = WorkspaceWriteAdapter(tool)

    description = _path_description(adapter.card)
    assert description.startswith("Path relative to the office directory.")
    assert "Absolute path of the file to write" not in description
    assert _path_description(tool.card) == "Absolute path of the file to write"


def test_absolute_path_inside_the_office_becomes_relative(tmp_path):
    office = tmp_path / "office"
    nested = office / "资料"
    nested.mkdir(parents=True)
    target = nested / "周报.md"

    assert office_relative_file_path(str(target), office) == "资料/周报.md"
    assert office_relative_file_path(str(office / "test.txt"), office) == "test.txt"
    assert office_relative_file_path("test.txt", office) is None
    assert office_relative_file_path(str(tmp_path / "outside.txt"), office) is None
    assert office_relative_file_path("C:/office/test.txt", office) is None


def test_absolute_symlink_that_leaves_the_office_stays_absolute(tmp_path):
    office = tmp_path / "office"
    office.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("secret", encoding="utf-8")
    link = office / "leak.txt"
    link.symlink_to(outside)

    assert office_relative_file_path(str(link), office) is None


def test_json_arguments_replace_only_an_in_office_absolute_path(tmp_path):
    office = tmp_path / "office"
    office.mkdir()
    inside = json.dumps({"file_path": str(office / "test.txt"), "content": "hello world\n"})
    outside = json.dumps({"file_path": "/tmp/test.txt", "content": "hello world\n"})

    rewritten = json.loads(rewrite_tool_args(inside, office))
    assert rewritten == {"file_path": "test.txt", "content": "hello world\n"}
    assert rewrite_tool_args(outside, office) == outside
    assert rewrite_tool_args({"file_path": "test.txt"}, office) == {"file_path": "test.txt"}


def test_write_preflight_sees_the_office_relative_path(tmp_path):
    office = tmp_path / "office"
    office.mkdir()

    class _Inputs:
        tool_name = "write_file"
        tool_args = {"file_path": str(office / "test.txt"), "content": "hello world\n"}

    class _Context:
        inputs = _Inputs()

    class _Access:
        _root = office

    class _Rail:
        workspace_access = _Access()

    ctx = _Context()
    rewrite_office_absolute_argument(_Rail(), ctx)
    assert ctx.inputs.tool_args["file_path"] == "test.txt"
    assert ctx.inputs.tool_args["content"] == "hello world\n"
