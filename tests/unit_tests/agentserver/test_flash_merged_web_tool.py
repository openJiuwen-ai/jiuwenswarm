from __future__ import annotations

import pytest
from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from jiuwenswarm.agents.harness.flash.tools import web_flash


class _ChildTool:
    def __init__(self, label: str, calls: list[tuple[str, dict]]) -> None:
        self._label = label
        self._calls = calls

    async def invoke(self, inputs: dict):
        self._calls.append((self._label, inputs))
        return {"tool": self._label, "inputs": inputs}


def test_web_flash_schema_requires_exactly_one_action() -> None:
    schema = web_flash._input_params("cn")
    validate = Draft202012Validator(schema).validate

    validate({"search": {"query": "current news"}})
    validate({"fetch": {"url": ["https://example.com"]}})

    invalid = [
        {},
        {"search": {"query": "q"}, "fetch": {"url": ["https://example.com"]}},
        {"search": {}},
        {"fetch": {"url": []}},
        {"fetch": {"url": "https://example.com"}},
        {"search": {"query": "q", "provider": "paid"}},
    ]
    for inputs in invalid:
        with pytest.raises(ValidationError):
            validate(inputs)


@pytest.mark.asyncio
async def test_web_flash_dispatches_to_mainline_backends(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        web_flash,
        "JiuwenHarnessWebSearchTool",
        lambda **_kwargs: _ChildTool("search", calls),
    )
    monkeypatch.setattr(
        web_flash,
        "JiuwenHarnessFetchWebpageTool",
        lambda **_kwargs: _ChildTool("fetch", calls),
    )
    tool = web_flash.build_web_flash_tool(agent_id="agent", language="cn")

    assert await tool.invoke({"search": {"query": "q"}}) == {
        "tool": "search",
        "inputs": {"query": "q"},
    }
    assert await tool.invoke({"fetch": {"url": ["https://example.com"]}}) == {
        "tool": "fetch",
        "inputs": {"url": ["https://example.com"]},
    }
    assert [name for name, _inputs in calls] == ["search", "fetch"]


@pytest.mark.asyncio
async def test_web_flash_runtime_rejects_multiple_actions(monkeypatch) -> None:
    calls: list[tuple[str, dict]] = []
    monkeypatch.setattr(
        web_flash,
        "JiuwenHarnessWebSearchTool",
        lambda **_kwargs: _ChildTool("search", calls),
    )
    monkeypatch.setattr(
        web_flash,
        "JiuwenHarnessFetchWebpageTool",
        lambda **_kwargs: _ChildTool("fetch", calls),
    )
    tool = web_flash.build_web_flash_tool(agent_id="agent")

    with pytest.raises(Exception):
        await tool.invoke({"search": {"query": "q"}, "fetch": {"url": ["u"]}})
    assert calls == []
