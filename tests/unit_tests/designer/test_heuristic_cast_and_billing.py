"""Cast aliases, solo sheets, time-window clips, and chat 402 demotion."""

from __future__ import annotations

import copy

import pytest

from jiuwenswarm.server.runtime.designer import model_tools
from jiuwenswarm.server.runtime.designer.handlers.image_nodes import _character_prompt
from jiuwenswarm.server.runtime.designer.script_analysis import (
    _heuristic_characters,
    heuristic_analysis,
)
from jiuwenswarm.server.runtime.designer.smart_graph import (
    apply_runtime_delegate,
    build_smart_video_graph,
)


DINNER = (
    "The Father's Reading: a close-up of the father in a navy sweater. "
    "The mother watches from her chair. A young child sits beside them. "
    "The child leans forward. The father stands."
)

DINNER_ANALYSIS = {
    "source": "llm",
    "characters": [
        {"id": "char_1", "name": "Father", "description": "navy sweater, grey trousers"},
        {"id": "char_2", "name": "Mother", "description": "green cardigan, dark skirt"},
        {"id": "char_3", "name": "Young child", "description": "yellow t-shirt, blue shorts"},
    ],
    "scenes": [{"id": "set_1", "name": "living room", "description": "dinner table at dusk"}],
    "shots": [
        {
            "shot_index": 1,
            "action": "The father reads a letter while the mother and child watch.",
            "camera": "close-up",
            "character_ids": ["char_1", "char_2", "char_3"],
            "setting_id": "set_1",
            "timeline": "0-5s",
        }
    ],
}


def test_article_and_bare_role_do_not_split_cast() -> None:
    names = [str(c.get("name") or "") for c in _heuristic_characters(DINNER)]
    low = [n.lower() for n in names]
    assert low.count("father") == 1
    assert "the father" not in low
    child_names = [n for n in low if "child" in n]
    assert child_names == ["young child"]
    assert "the child" not in low
    assert sum(1 for n in low if n == "mother") == 1


def test_leaving_man_stays_a_second_person() -> None:
    prompt = (
        "A man speaks at the front while another man gets up and leaves, "
        "and a woman listens with a child next to her."
    )
    names = [str(c.get("name") or "").lower() for c in _heuristic_characters(prompt)]
    assert any(n == "man" for n in names)
    assert any("leaving" in n or n.endswith(" 2") for n in names)
    assert any(n == "woman" for n in names)
    assert any("child" in n for n in names)


def test_solo_prompt_is_one_person_on_a_plain_backdrop() -> None:
    wrapped = _character_prompt(
        "Father\nCANONICAL postcard.\nStory context: dinner table, two people, living room."
    )
    assert "story context" not in wrapped.lower()
    assert "one person" in wrapped.lower()
    assert "studio backdrop" in wrapped.lower()
    graph = build_smart_video_graph(
        project_id="proj_solo",
        prompt=DINNER,
        analysis=copy.deepcopy(DINNER_ANALYSIS),
    )
    sheets = [
        n
        for n in graph["nodes"]
        if str(n.get("id") or "").startswith("n_character")
    ]
    assert len(sheets) == 3
    for node in sheets:
        prompt = str((node.get("config") or {}).get("prompt") or "")
        low = prompt.lower()
        assert "story context" not in low
        assert "one person only" in low
        assert "plain empty studio backdrop" in low
        assert "close-up" not in low
        assert "living room" not in low
        assert "dinner" not in low


def test_long_film_clips_use_time_windows_not_angles() -> None:
    prompt = (
        "Make a 30 second film. Father reads a letter at the table. "
        "Then the child stands and walks to the door. "
        "Then the mother speaks to the father."
    )
    analysis = {
        "source": "llm",
        "characters": [
            {"id": "char_1", "name": "Father", "description": "navy sweater"},
            {"id": "char_2", "name": "Child", "description": "yellow t-shirt"},
            {"id": "char_3", "name": "Mother", "description": "green cardigan"},
        ],
        "scenes": [{"id": "set_1", "name": "dining room", "description": "table and door"}],
        "shots": [
            {
                "shot_index": 1,
                "action": "Father reads a letter at the table.",
                "character_ids": ["char_1"],
                "setting_id": "set_1",
                "timeline": "0-10s",
            },
            {
                "shot_index": 2,
                "action": "The child stands and walks to the door.",
                "character_ids": ["char_2"],
                "setting_id": "set_1",
                "timeline": "10-20s",
            },
            {
                "shot_index": 3,
                "action": "The mother speaks to the father.",
                "character_ids": ["char_1", "char_3"],
                "setting_id": "set_1",
                "timeline": "20-30s",
            },
        ],
        "target_duration_sec": 30,
    }
    graph = build_smart_video_graph(
        project_id="proj_windows",
        prompt=prompt,
        analysis=analysis,
    )
    clips = [n for n in graph["nodes"] if str(n.get("id") or "").startswith("n_clip")]
    assert len(clips) >= 2
    actions = [str((n.get("config") or {}).get("shot_action") or "").strip() for n in clips]
    assert all(actions)
    assert len({a[:40] for a in actions}) >= 2
    for node in clips:
        cfg = node.get("config") or {}
        text = str((cfg.get("generate") or {}).get("prompt") or "").lower()
        assert "view left" not in text
        assert "view right" not in text
        assert str(cfg.get("view_key") or "") == ""
    assert [str((n.get("config") or {}).get("timeline") or "") for n in clips] == [
        "0-10s",
        "10-20s",
        "20-30s",
    ]


def test_402_marks_chat_unavailable_and_fails_closed() -> None:
    model_tools._chat_billing_block = ""
    try:
        assert model_tools.is_chat_payment_block("Error code: 402 Insufficient Balance")
        assert not model_tools.is_chat_payment_block("Wan size error 181004")
        assert model_tools.note_chat_model_unavailable(
            "Error code: 402 Insufficient Balance"
        )
        assert model_tools.llm_available() is False
        # Within the same turn, call_model_tool stays fail-closed on the latch.
        # require_llm (next user entry) clears it — covered separately.
        graph = build_smart_video_graph(
            project_id="proj_402",
            prompt="A father reads a letter.",
            analysis=heuristic_analysis("A father reads a letter."),
        )
        # Graph stamping stays blunt — no demote-to-handler and no nested
        # require_llm. Billing is enforced at call_model_tool until the next entry.
        stamped = apply_runtime_delegate(graph)
        assert any(
            str((n.get("config") or {}).get("delegate")) == "agent"
            for n in stamped["nodes"]
        )
    finally:
        model_tools._chat_billing_block = ""


@pytest.mark.asyncio
async def test_call_model_tool_does_not_retry_after_402() -> None:
    """Same user action: after 402, later model calls short-circuit."""
    model_tools._chat_billing_block = ""
    try:
        model_tools.note_chat_model_unavailable("Error code: 402 Insufficient Balance")
        result = await model_tools.call_model_tool(
            prompt="hello",
            system="reply",
        )
        assert result["ok"] is False
        assert result["unavailable"] is True
    finally:
        model_tools._chat_billing_block = ""


@pytest.mark.asyncio
async def test_require_llm_allows_retry_after_402_recharge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After recharge, the next chat/Play entry must probe again (no process-lifetime latch)."""
    model_tools._chat_billing_block = ""
    try:
        model_tools.note_chat_model_unavailable("Error code: 402 Insufficient Balance")
        blocked = await model_tools.call_model_tool(
            prompt="hello",
            system="reply",
        )
        assert blocked["ok"] is False
        assert blocked["unavailable"] is True

        monkeypatch.setattr(model_tools, "llm_available", lambda: True)
        model_tools.require_llm()
        assert model_tools.chat_model_billing_block() == ""

        # Latch cleared: must not short-circuit as billing-unavailable.
        monkeypatch.setattr(model_tools, "list_configured_models", lambda: [])
        monkeypatch.delenv("API_KEY", raising=False)
        monkeypatch.delenv("OPENAI_API_KEY", raising=False)
        monkeypatch.delenv("API_BASE", raising=False)
        monkeypatch.delenv("OPENAI_API_BASE", raising=False)
        result = await model_tools.call_model_tool(
            prompt="hello",
            system="reply",
        )
        assert result["ok"] is False
        assert result.get("unavailable") is not True
        assert result["code"] == model_tools.LLM_NOT_CONFIGURED
    finally:
        model_tools._chat_billing_block = ""
