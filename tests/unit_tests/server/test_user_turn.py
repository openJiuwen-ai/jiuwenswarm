# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The single user-turn renderer shared by single-agent and team runs."""

from __future__ import annotations

import json
import re

from jiuwenswarm.server.runtime.agent_adapter.user_turn import UserTurn


def _envelope(rendered: str) -> dict:
    """Parse the JSON envelope out of a rendered prompt."""
    return json.loads(rendered[rendered.index("{"):])


def _turn(**overrides) -> UserTurn:
    base = {
        "text": "总结这份文档",
        "channel": "web",
        "language": "zh",
        "files": {},
    }
    base.update(overrides)
    return UserTurn(**base)


def test_render_carries_uploaded_files():
    files = {"uploaded_documents": [{"filename": "需求.md", "path": "/uploads/需求.md"}]}

    envelope = _envelope(_turn(files=files).render())

    assert json.loads(envelope["files_updated_by_user"]) == files
    assert envelope["content"] == "总结这份文档"


def test_render_is_identical_for_the_same_turn_regardless_of_caller():
    # Team delivers ``turn.render()`` exactly like the single-agent path, so the
    # envelope a leader sees must match field for field.
    turn = _turn(files={"uploaded_images": [{"filename": "a.png", "path": "/uploads/a.png"}]})

    single_agent = _envelope(turn.render())
    team = _envelope(turn.with_text(turn.text).render())

    single_agent.pop("timestamp")
    team.pop("timestamp")
    assert single_agent == team


def test_with_text_rewrites_content_and_keeps_context():
    files = {"uploaded_documents": [{"filename": "需求.md", "path": "/uploads/需求.md"}]}
    turn = _turn(files=files)

    envelope = _envelope(turn.with_text("$reviewer 看一下").render())

    assert envelope["content"] == "$reviewer 看一下"
    assert json.loads(envelope["files_updated_by_user"]) == files


def test_with_text_does_not_mutate_the_original_turn():
    turn = _turn()

    turn.with_text("改写后的内容")

    assert turn.text == "总结这份文档"


def test_render_reports_sender_and_chat_type():
    metadata = {"sender_name": "张三", "im_chat_type": "group"}

    envelope = _envelope(_turn(metadata=metadata).render())

    assert envelope["sender"] == "张三"
    assert envelope["chat_type"] == "group"


def test_render_marks_system_channels_and_drops_files():
    files = {"uploaded_documents": [{"filename": "需求.md", "path": "/uploads/需求.md"}]}

    envelope = _envelope(_turn(channel="cron", files=files).render())

    assert envelope["source"] == "system"
    assert envelope["type"] == "cron"
    assert "files_updated_by_user" not in envelope


def test_render_includes_trusted_dirs_and_skills():
    turn = _turn(trusted_dirs=["/work/project"], skills=["doc"])

    envelope = _envelope(turn.render())

    assert json.loads(envelope["trusted_dirs"]) == ["/work/project"]
    assert envelope["skills_to_use"] == ["doc"]


def test_render_parses_skills_from_text_when_not_declared():
    envelope = _envelope(_turn(text="/skills use doc, 帮我写文档").render())

    assert envelope["skills_to_use"] == ["doc"]
    # The text is never stripped — the message must stay readable.
    assert "帮我写文档" in envelope["content"]


def test_render_carries_the_clock_in_the_message_not_the_system_prompt():
    # The system prompt states no date on purpose: it precedes the conversation,
    # so a value that ticks between calls invalidates the KV-cache prefix. The
    # envelope is the newest message at the end of the context, so the same
    # value costs nothing there. Every rendered turn must therefore carry it.
    envelope = _envelope(_turn().render())

    assert envelope["timezone"] == "Asia/Shanghai"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", envelope["timestamp"])


def test_system_channel_turns_carry_the_clock_too():
    # A cron turn is the one most likely to need a date, and no person typed it.
    envelope = _envelope(_turn(channel="cron").render())

    assert envelope["timezone"] == "Asia/Shanghai"
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", envelope["timestamp"])


def test_render_passes_through_non_text_payloads():
    marker = object()

    assert _turn(text=marker).render() is marker


def test_resume_payloads_are_not_given_a_clock():
    # An ``InteractiveInput`` resuming an interrupt is its own payload and is
    # matched structurally by the agent framework; there is no field to state a
    # clock in without changing what the framework receives. It is also the one
    # turn that does not need one -- the turn it resumes stated a timestamp
    # moments earlier and it is still in context.
    #
    # The pass-through test above already pins identity, but it passes an
    # ``object()``, which has no ``__dict__`` and so cannot express the way a
    # clock would plausibly be added here: written onto the payload in place and
    # the same object returned. This one is mutable, so the attribute check bites.
    class _Resume:
        pass

    payload = _Resume()
    before = dict(vars(payload))

    assert _turn(text=payload).render() is payload
    assert vars(payload) == before


def test_render_prefixes_interaction_context():
    turn = _turn(metadata={"interaction_context": "上一轮被中断"})

    rendered = turn.render()

    assert rendered.startswith("\n上一轮被中断\n\n")
