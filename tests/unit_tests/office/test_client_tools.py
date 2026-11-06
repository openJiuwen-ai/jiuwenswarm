# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""What a model call is offered, and how a PowerPoint tool call pauses for the task pane and resumes with its result.

Each test runs a conversation's real DeepAgent with the PowerPoint rail and a
scripted model (``conversation``), through the pause and resume agent-core
gives client tools.
"""

from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest
from openjiuwen.core.foundation.llm import ToolCall
from openjiuwen.core.foundation.tool import ToolExposure

from jiuwenswarm.agents.harness.common.rails.tool_retrieval_prompt_rail import ToolRetrievalPromptRail
from jiuwenswarm.agents.harness.common.tools.tool_retrieval_toolkits import ToolRetrievalToolkit
from jiuwenswarm.common.file_download_token import validate_file_download_token
from jiuwenswarm.office.assets import OfficeAssetStore
from jiuwenswarm.office.contract import POWERPOINT_PACKAGE, load_contract

PNG_BASE64 = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR4nGNgYGD4DwABBAEAwS2OUAAAAABJRU5ErkJggg=="
READ_CONTEXT = ToolCall(id="context-1", type="function", name="read_presentation_context", arguments="{}")


def _render(call_id: str = "render-1") -> ToolCall:
    return ToolCall(id=call_id, type="function", name="read_slide_image", arguments='{"slide_id": "256#1"}')


def _text(text: str) -> dict:
    return {"content": [{"type": "text", "text": text}]}


def _deck() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("[Content_Types].xml", "<x/>")
        archive.writestr("ppt/presentation.xml", "<x/>")
    return buffer.getvalue()


def _offered_powerpoint_tools(names: set[str]) -> set[str]:
    return names & {tool.name for tool in load_contract(POWERPOINT_PACKAGE)}


async def test_a_model_call_is_offered_the_tools_the_presentation_s_powerpoint_runs(conversation, attach_pane):
    attach_pane("deck-a", ["read_presentation_context", "presentation_apply_patch"])

    async with conversation([]) as pane:
        await pane.turn("hello")
        _messages, offered = pane.model.seen[-1]
        assert _offered_powerpoint_tools(offered) == {"read_presentation_context", "presentation_apply_patch"}
        assert "list_open_documents" in offered

        # The task pane attaches again from a PowerPoint that runs more.
        attach_pane("deck-a", ["read_presentation_context", "read_slide_image"])
        await pane.turn("hello again")
        _messages, offered = pane.model.seen[-1]
        assert _offered_powerpoint_tools(offered) == {"read_presentation_context", "read_slide_image"}


async def test_a_call_pauses_with_its_arguments_and_links_to_the_assets_they_name(conversation, attach_pane):
    attach_pane("deck-a", ["read_presentation_context", "import_slides"])
    descriptor = OfficeAssetStore().put("deck-a", _deck()).descriptor()
    arguments = {"asset": descriptor, "source_positions": [2]}
    call = ToolCall(id="import-1", type="function", name="import_slides", arguments=json.dumps(arguments))

    async with conversation([[call]]) as pane:
        [request] = await pane.turn("import slide 2 of the template")
        assert (request["request_id"], request["tool_name"], request["arguments"]) == (
            "import-1", "import_slides", arguments)
        [(handle, url)] = request["client_tool"]["downloads"].items()
        assert handle == descriptor["handle"]
        token = parse_qs(urlparse(url).query)["token"][0]
        assert validate_file_download_token(token, session_id="deck-a")["path"].endswith(f"{handle}.bin")

        assert await pane.answer((request, _text('{"success": true, "inserted": 1}'))) == []
        assert pane.model.tool_results() == {"import-1": '{"success": true, "inserted": 1}'}
        # No model call, the one after the result included, carries a link.
        assert len(pane.model.seen) == 2
        assert "/file-api/download" not in json.dumps([[str(m.content) for m in seen] for seen, _ in pane.model.seen])


async def test_a_call_naming_another_conversation_s_asset_fails_without_pausing(conversation, attach_pane):
    attach_pane("deck-a", ["import_slides"])
    descriptor = OfficeAssetStore().put("deck-b", _deck()).descriptor()
    call = ToolCall(id="import-1", type="function", name="import_slides", arguments=json.dumps({"asset": descriptor}))

    async with conversation([[call]]) as pane:
        assert await pane.turn("import the template") == []
        assert "publish the file with publish_powerpoint_asset first" in pane.model.tool_results()["import-1"]


@pytest.mark.parametrize(("supported", "live", "reason"), [
    (["read_presentation_context"], True, "cannot run read_slide_image"),
    (["read_slide_image"], False, "not open in PowerPoint right now"),
    (None, True, "no PowerPoint presentation is attached"),
])
async def test_a_call_no_task_pane_can_run_now_fails_without_pausing(
        conversation, attach_pane, supported, live, reason):
    if supported is not None:
        attach_pane("deck-a", supported, live=live)

    async with conversation([[_render()]]) as pane:
        assert await pane.turn("show me slide 1") == []
        assert reason in pane.model.tool_results()["render-1"]


async def test_a_render_reaches_a_vision_model_in_one_model_call(conversation, attach_pane):
    attach_pane("deck-a", ["read_presentation_context", "read_slide_image"])
    rendered = {"content": [{"type": "text", "text": '{"slide_index": 0}'},
                            {"type": "image", "source": {"type": "base64", "media_type": "image/png",
                                                         "data": PNG_BASE64}}]}

    async with conversation([[_render()], [READ_CONTEXT]], native_images=True) as pane:
        [render] = await pane.turn("show me slide 1")
        [context] = await pane.answer((render, rendered))
        assert pane.model.image_urls(-1) == [f"data:image/png;base64,{PNG_BASE64}"]

        await pane.answer((context, _text("{}")))
        assert pane.model.image_urls(-1) == []


async def test_a_render_for_a_model_without_vision_is_kept_in_the_conversation_s_uploads(
        conversation, attach_pane, sessions):
    attach_pane("deck-a", ["read_slide_image"])
    rendered = {"content": [{"type": "text", "text": '{"slide_index": 0}'},
                            {"type": "image", "data_url": f"data:image/png;base64,{PNG_BASE64}"}]}

    async with conversation([[_render()]]) as pane:
        [render] = await pane.turn("show me slide 1")
        await pane.answer((render, rendered))

        result = pane.model.tool_results()["render-1"]
        assert result.startswith('{"slide_index": 0}') and "visual_question_answering" in result
        path = Path(result.split("visual_attachment.path: ", 1)[1].splitlines()[0])
        assert path.parent == sessions / "deck-a" / "uploads" and path.read_bytes().startswith(b"\x89PNG")
        assert all(not pane.model.image_urls(call) for call in range(len(pane.model.seen)))


@pytest.mark.parametrize(("result", "expected"), [
    ({"content": [{"type": "text", "text": "slide 256#1 not found"}], "is_error": True}, "slide 256#1 not found"),
    ({"status": "client_gone"}, "disconnected"),
    ({"status": "timeout"}, "did not return a result in time"),
])
async def test_a_failed_call_tells_the_model_how_it_ended(conversation, attach_pane, result, expected):
    attach_pane("deck-a", ["read_slide_image"])

    async with conversation([[_render()]], native_images=True) as pane:
        [render] = await pane.turn("show me slide 1")
        assert await pane.answer((render, result)) == []
        assert expected in pane.model.tool_results()["render-1"]


async def test_a_deferred_tool_called_through_tool_call_pauses_and_resumes_the_same_way(conversation, attach_pane):
    """JiuwenSwarm offers deferred tools through ``tool_search`` and runs them through the ``tool_call`` envelope."""
    attach_pane("deck-a", ["read_presentation_context", "duplicate_slide"])
    call = ToolCall(id="envelope-1", type="function", name="tool_call",
                    arguments=json.dumps({"name": "duplicate_slide", "args": {"slide_id": "256#1"}}))
    pane = conversation([[call]])
    toolkit = ToolRetrievalToolkit(deferred_tools_provider=lambda: [
        card.tool_info() for card in pane.agent.ability_manager.list()
        if getattr(card, "exposure", None) is ToolExposure.DEFERRED])
    rail = ToolRetrievalPromptRail(toolkit=toolkit, config_base={
        "progressive_tool_enabled": True, "progressive_tool_optimize": True})

    async with pane:
        await pane.agent.register_rail(rail)
        [request] = await pane.turn("duplicate slide 1")
        assert (request["tool_name"], request["arguments"]) == ("duplicate_slide", {"slide_id": "256#1"})

        assert await pane.answer((request, _text('{"new_slide_id": "257#2"}'))) == []
        assert "257#2" in pane.model.tool_results()["envelope-1"]
