from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.ppt_common import PptCommon
from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils import BashResult

_OUTLINE = """### P1: 封面
- **类型**：cover
- **研究需求**：✅
- **标题**：中国平安投资分析
"""


class _Node:
    def __init__(self, *, read_payload: Any = "") -> None:
        self.read_payload = read_payload
        self.calls: list[str] = []

    def has_tool(self, name: str) -> bool:
        return name in {"read_file", "bash"}

    async def call_tool(self, name: str, **_kwargs: Any) -> Any:
        self.calls.append(name)
        if name == "read_file":
            return self.read_payload
        raise AssertionError(name)


@pytest.mark.asyncio
async def test_read_file_falls_back_to_bash_cat_when_tool_returns_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _Node(read_payload=SimpleNamespace(success=True, data={"content": ""}))

    async def fake_run_bash(_node: Any, command: str, **_kwargs: Any) -> BashResult:
        assert "cat" in command
        return BashResult(exit_code=0, stdout=_OUTLINE, stderr="", raw="")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils.run_bash",
        fake_run_bash,
    )
    text = await PptCommon.read_file(node, "/tmp/outline.md", label="outline.md")
    assert "中国平安投资分析" in text
    assert node.calls == ["read_file"]


@pytest.mark.asyncio
async def test_read_file_stays_empty_when_tool_and_bash_are_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _Node(read_payload="")

    async def fake_run_bash(_node: Any, command: str, **_kwargs: Any) -> BashResult:
        return BashResult(exit_code=1, stdout="", stderr="missing", raw="")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils.run_bash",
        fake_run_bash,
    )
    text = await PptCommon.read_file(node, "/tmp/outline.md", required=False, label="outline.md")
    assert text == ""


@pytest.mark.asyncio
async def test_p6_prepare_succeeds_after_empty_read_file_bash_fallback(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.deep_research import (
        PrepareNode,
    )

    node = PrepareNode()
    monkeypatch.setattr(node, "has_tool", lambda name: name in {"read_file", "bash"})

    async def call_tool(name: str, **_kwargs: Any) -> Any:
        if name == "read_file":
            return ""
        raise AssertionError(name)

    monkeypatch.setattr(node, "call_tool", call_tool)

    async def fake_run_bash(_node: Any, command: str, **_kwargs: Any) -> BashResult:
        assert "outline.md" in command
        return BashResult(exit_code=0, stdout=_OUTLINE, stderr="", raw="")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils.run_bash",
        fake_run_bash,
    )

    async def fake_parse(_self: Any, outline_text: str) -> list[dict[str, Any]]:
        assert "中国平安" in outline_text
        return [
            {
                "page_number": 1,
                "title": "中国平安投资分析",
                "page_type": "cover",
                "research_queries": ["估值"],
                "data_needs": [],
            }
        ]

    async def fake_should_search(_self: Any, *_args: Any, **_kwargs: Any) -> bool:
        return False

    monkeypatch.setattr(PrepareNode, "_parse_outline_pages", fake_parse)
    monkeypatch.setattr(PrepareNode, "_should_search", fake_should_search)

    result = await node._execute({"output_dir": str(tmp_path), "search_mode": "no_search"})
    assert result["prepare_status"] == "ok"
    assert len(result["pages"]) == 1


@pytest.mark.asyncio
async def test_p6_prepare_fails_when_outline_still_empty(
    tmp_path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.deep_research import (
        PrepareNode,
    )

    node = PrepareNode()
    monkeypatch.setattr(node, "has_tool", lambda name: name == "read_file")

    async def call_tool(name: str, **_kwargs: Any) -> Any:
        if name == "read_file":
            return ""
        raise AssertionError(name)

    monkeypatch.setattr(node, "call_tool", call_tool)
    result = await node._execute({"output_dir": str(tmp_path)})
    assert result == {"prepare_status": "failed"}


def test_parse_tool_file_content_drops_failure_envelope_and_empty_warning() -> None:
    failed = SimpleNamespace(success=False, error="File content exceeds maximum allowed size")
    assert PptCommon.parse_tool_file_content(failed) == ""
    assert PptCommon.tool_result_error(failed) == "File content exceeds maximum allowed size"
    assert (
        PptCommon.parse_tool_file_content(
            "Warning: the file exists but the contents are empty."
        )
        == ""
    )
    assert "中国平安" in PptCommon.parse_tool_file_content(
        SimpleNamespace(success=True, data={"content": _OUTLINE})
    )


@pytest.mark.asyncio
async def test_read_file_falls_back_to_bash_when_tool_returns_failure_envelope(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    node = _Node(
        read_payload=SimpleNamespace(
            success=False, error="sandbox read failed"
        )
    )

    async def fake_run_bash(_node: Any, command: str, **_kwargs: Any) -> BashResult:
        assert "cat" in command
        return BashResult(exit_code=0, stdout=_OUTLINE, stderr="", raw="")

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.skill_turbo.skill_codes.ppt.utils.bash_utils.run_bash",
        fake_run_bash,
    )
    text = await PptCommon.read_file(node, "/tmp/outline.md", label="outline.md")
    assert "中国平安投资分析" in text


@pytest.mark.asyncio
async def test_read_file_required_raises_read_error_not_empty_when_envelope_and_no_bash() -> None:
    class _NoBash(_Node):
        def has_tool(self, name: str) -> bool:
            return name == "read_file"

    node = _NoBash(
        read_payload=SimpleNamespace(success=False, error="lock timeout")
    )
    with pytest.raises(RuntimeError, match="读取 outline.md 失败") as exc_info:
        await PptCommon.read_file(
            node, "/tmp/outline.md", required=True, label="outline.md"
        )
    assert "为空或不存在" not in str(exc_info.value)
    assert "lock timeout" in str(exc_info.value)


@pytest.mark.asyncio
async def test_write_file_raises_when_tool_returns_failure_envelope() -> None:
    class _WriteNode:
        def has_tool(self, name: str) -> bool:
            return name == "write_file"

        async def call_tool(self, name: str, **_kwargs: Any) -> Any:
            assert name == "write_file"
            return SimpleNamespace(
                success=False,
                error="File has not been read yet.",
            )

    with pytest.raises(RuntimeError, match="写入 outline.md 失败") as exc_info:
        await PptCommon.write_file(
            _WriteNode(), "/tmp/outline.md", "### P1: 封面\n", label="outline.md"
        )
    assert "File has not been read yet." in str(exc_info.value)


@pytest.mark.asyncio
async def test_write_file_succeeds_when_tool_reports_success() -> None:
    class _WriteNode:
        def has_tool(self, name: str) -> bool:
            return name == "write_file"

        async def call_tool(self, name: str, **kwargs: Any) -> Any:
            assert kwargs["file_path"].endswith("outline.md")
            assert "封面" in kwargs["content"]
            return SimpleNamespace(success=True, data={"file_path": kwargs["file_path"]})

    path = await PptCommon.write_file(
        _WriteNode(), "/tmp/outline.md", "### P1: 封面\n", label="outline.md"
    )
    assert str(path).endswith("outline.md")
