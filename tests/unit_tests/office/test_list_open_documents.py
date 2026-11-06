# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``list_open_documents``: the user's attached documents, and the conversation each is attached to."""

from __future__ import annotations

import json
from types import SimpleNamespace

from jiuwenswarm.office.documents import ListOpenDocumentsTool


async def _list(session_id: str) -> dict:
    tool = ListOpenDocumentsTool()
    return json.loads(await tool.invoke({}, session=SimpleNamespace(get_session_id=lambda: session_id)))


async def test_a_conversation_sees_its_own_document_and_the_user_s_others(attach_pane):
    attach_pane("deck-a", ["read_presentation_context"], name="Roadmap.pptx")
    attach_pane("deck-b", ["read_presentation_context"], name="budget.pptx")
    attach_pane("deck-c", ["read_presentation_context"], name="Theirs.pptx", owner="user-2")

    assert await _list("deck-a") == {
        "current_document": "Roadmap.pptx",
        "other_documents": [{"display_name": "budget.pptx", "kind": "presentation", "host_vendor": "microsoft",
                             "session_id": "deck-b"}],
    }
    assert (await _list("deck-c"))["other_documents"] == []


async def test_a_conversation_whose_document_closed_sees_none(attach_pane):
    attach_pane("deck-a", ["read_presentation_context"], live=False)
    attach_pane("deck-b", ["read_presentation_context"])

    assert await _list("deck-a") == {"current_document": "", "other_documents": []}
