# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Director JSON phases tolerate messy model replies and re-ask once before giving up."""

from __future__ import annotations

import json

import pytest

from jiuwenswarm.server.runtime.designer import orchestration
from jiuwenswarm.server.runtime.designer.model_tools import DesignerLlmError
from jiuwenswarm.server.runtime.designer.script_analysis import _extract_json_object

_BRIEF = "# Brief\n\nLead opens the door and greets the guest."


def _review_reply(markdown: str) -> str:
    return json.dumps(
        {"ok": True, "patched_brief_markdown": markdown, "notes": "patched", "issues": []}
    )


def test_extract_json_object_accepts_raw_newlines_in_strings() -> None:
    text = '{"ok":true,"patched_brief_markdown":"# Title\n- shot 1","notes":"n"}'

    assert _extract_json_object(text) == {
        "ok": True,
        "patched_brief_markdown": "# Title\n- shot 1",
        "notes": "n",
    }


def test_extract_json_object_skips_reasoning_block() -> None:
    text = '<think>Plan {ok, patched_brief_markdown} first.</think>\n{"ok":true,"notes":"n"}'

    assert _extract_json_object(text) == {"ok": True, "notes": "n"}


def test_extract_json_object_skips_reasoning_without_opening_tag() -> None:
    assert _extract_json_object('Plan {draft}.</think>{"ok":true}') == {"ok": True}


def test_extract_json_object_keeps_fences_inside_string_values() -> None:
    text = '```json\n{"ok":true,"patched_brief_markdown":"see ```code``` here"}\n```'

    assert _extract_json_object(text) == {
        "ok": True,
        "patched_brief_markdown": "see ```code``` here",
    }


def test_extract_json_object_rejects_truncated_reply() -> None:
    assert _extract_json_object('{"ok":true,"patched_brief_markdown":"# cut') is None


def _scripted_model(monkeypatch: pytest.MonkeyPatch, replies: list[str]) -> list[str]:
    systems: list[str] = []

    async def fake_model_call(**kwargs: object) -> dict[str, object]:
        systems.append(str(kwargs["system"]))
        return {"ok": True, "text": replies[len(systems) - 1]}

    monkeypatch.setattr(orchestration, "call_model_tool", fake_model_call)
    return systems


def _brief_graph() -> dict:
    return {
        "description": "Lead opens the door.",
        "nodes": [],
        "metadata": {
            "approved_brief": _BRIEF,
            "script_analysis": {"characters": [{"id": "char_1", "name": "Lead"}]},
        },
    }


@pytest.mark.asyncio
async def test_brief_review_reasks_once_after_unparseable_reply(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    patched = (
        "# Brief\n\nLead opens the door, smiles, and greets the guest warmly "
        "before both step into the lamp-lit hallway."
    )
    systems = _scripted_model(monkeypatch, ["Let me review the brief first.", _review_reply(patched)])
    graph = _brief_graph()

    await orchestration.Director().review_brief(graph)

    assert len(systems) == 2
    assert orchestration._JSON_RETRY_RULE not in systems[0]
    assert systems[1].endswith(orchestration._JSON_RETRY_RULE)
    assert graph["metadata"]["approved_brief"].startswith(patched)
    assert "review_skipped" not in graph["metadata"]["director_brief_ack"]


@pytest.mark.asyncio
async def test_brief_review_keeps_authored_brief_when_replies_never_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systems = _scripted_model(monkeypatch, ["not json", '{"ok":true,"patched_brief_markdown":"# cut'])
    graph = _brief_graph()

    await orchestration.Director().review_brief(graph)

    assert len(systems) == 2
    assert graph["metadata"]["approved_brief"].startswith(_BRIEF)
    assert graph["metadata"]["director_brief_ack"]["review_skipped"] == "invalid_json"


@pytest.mark.asyncio
async def test_storyboard_review_keeps_shots_when_replies_never_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systems = _scripted_model(monkeypatch, ["not json", "still not json"])
    graph = {
        "description": "Lead opens the door.",
        "nodes": [],
        "metadata": {
            "script_analysis": {
                "clips": [{"clip_index": 1, "timeline": "0-5s", "action": "Lead opens the door."}]
            }
        },
    }

    ack = await orchestration.Director().review_storyboard_once(graph, node_states=None)

    assert len(systems) == 2
    assert ack["review_skipped"] == "invalid_json"
    clips = graph["metadata"]["script_analysis"]["clips"]
    assert clips[0]["action"] == "Lead opens the door."


@pytest.mark.asyncio
async def test_execution_graph_still_fails_when_replies_never_parse(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    systems = _scripted_model(monkeypatch, ["not json", "still not json"])
    graph = {
        "description": "Lead opens the door.",
        "nodes": [],
        "metadata": {
            "script_analysis": {
                "source": "llm",
                "characters": [{"id": "char_1", "name": "Lead"}],
                "scenes": [{"id": "set_1", "name": "Room"}],
                "clips": [{"clip_index": 1, "setting_id": "set_1", "action": "Open door"}],
            }
        },
    }

    with pytest.raises(DesignerLlmError, match="execution graph JSON"):
        await orchestration.Director().design_execution_graph(graph)
    assert len(systems) == 2
