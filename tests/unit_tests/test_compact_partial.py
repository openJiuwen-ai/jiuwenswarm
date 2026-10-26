from __future__ import annotations

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _write_history(path: Path, records: list[dict]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.suffix == ".jsonl":
        payload = "\n".join(json.dumps(record, ensure_ascii=False) for record in records)
        if payload:
            payload += "\n"
        path.write_text(payload, encoding="utf-8")
        return
    path.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_history(path: Path) -> list[dict]:
    deadline = time.time() + 5
    while time.time() < deadline:
        if path.exists():
            if path.suffix == ".jsonl":
                data = [
                    json.loads(line)
                    for line in path.read_text(encoding="utf-8").splitlines()
                    if line.strip()
                ]
            else:
                data = json.loads(path.read_text(encoding="utf-8"))
            if data:
                return data
        time.sleep(0.05)
    if not path.exists():
        return []
    if path.suffix == ".jsonl":
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
    return json.loads(path.read_text(encoding="utf-8"))


def _make_user_record(index: int, content: str | None = None, ts: float | None = None) -> dict:
    content = content or f"user message {index}"
    return {
        "id": f"u{index}",
        "role": "user",
        "content": content,
        "request_id": f"r{index}",
        "timestamp": ts if ts is not None else float(index),
    }


def _make_assistant_record(
    index: int,
    content: str | None = None,
    event_type: str | None = None,
    ts: float | None = None,
) -> dict:
    content = content or f"assistant response {index}"
    return {
        "id": f"a{index}",
        "role": "assistant",
        "content": content,
        "request_id": f"r{index}",
        "timestamp": ts if ts is not None else float(index) + 0.5,
        "event_type": event_type,
    }


# ---------------------------------------------------------------------------
# compact_partial_session  tests
# ---------------------------------------------------------------------------
def _setup_compact_patches(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_agent_sessions_dir",
        lambda: tmp_path,
    )
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata._enqueue_write",
        lambda *args, **kwargs: None,
    )


# ---------------------------------------------------------------------------
# compact_partial_session  tests
# ---------------------------------------------------------------------------
class TestCompactPartialSessionFrom:
    @staticmethod
    def test_compact_from_direction_truncates_history(tmp_path, monkeypatch):
        _setup_compact_patches(monkeypatch, tmp_path)

        records = [
            _make_user_record(1, "first question"),
            _make_assistant_record(1, "first answer"),
            _make_user_record(2, "second question"),
            _make_assistant_record(2, "second answer"),
            _make_user_record(3, "third question"),
            _make_assistant_record(3, "third answer"),
        ]
        _write_history((tmp_path / "s1" / "history.jsonl"), records)

        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        result = compact_partial_session(session_id="s1", turn_index=2, direction="from")

        assert result["direction"] == "from"
        assert result["turn_index"] == 2
        assert result["summarized_messages"] == 4
        assert result["removed_records"] == 4
        assert "second question" in result["content"]

        final_history = _read_history(tmp_path / "s1" / "history.jsonl")
        event_types = [r.get("event_type") for r in final_history]
        assert "context.compact_boundary" in event_types
        assert "context.rewind_summary" in event_types

    @staticmethod
    def test_compact_from_with_llm_summary(tmp_path, monkeypatch):
        _setup_compact_patches(monkeypatch, tmp_path)

        records = [
            _make_user_record(1, "hello"),
            _make_assistant_record(1, "hi there"),
        ]
        _write_history((tmp_path / "s2" / "history.jsonl"), records)

        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        result = compact_partial_session(
            session_id="s2",
            turn_index=1,
            direction="from",
            llm_summary="LLM generated summary text",
        )

        final_history = _read_history(tmp_path / "s2" / "history.jsonl")
        event_types = [r.get("event_type") for r in final_history]
        assert event_types == [
            "context.compact_boundary",
            "context.rewind_summary",
            "context.compact_summary",
        ]
        compact_entry = final_history[2]
        assert compact_entry["content"] == "LLM generated summary text"
        assert compact_entry["is_compact_summary"] is True
        assert compact_entry["transcript_only"] is True


class TestCompactPartialSessionUpTo:
    @staticmethod
    def test_compact_up_to_direction_keeps_later_turns(tmp_path, monkeypatch):
        _setup_compact_patches(monkeypatch, tmp_path)

        records = [
            _make_user_record(1, "first question"),
            _make_assistant_record(1, "first answer"),
            _make_user_record(2, "second question"),
            _make_assistant_record(2, "second answer"),
            _make_user_record(3, "third question"),
            _make_assistant_record(3, "third answer"),
        ]
        _write_history((tmp_path / "s3" / "history.jsonl"), records)

        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        result = compact_partial_session(session_id="s3", turn_index=3, direction="up_to")

        assert result["direction"] == "up_to"
        assert result["removed_records"] == 4
        assert result["summarized_messages"] == 4
        assert "third question" in result["content"]

        final_history = _read_history(tmp_path / "s3" / "history.jsonl")
        event_types = [r.get("event_type") for r in final_history]
        assert "context.compact_boundary" in event_types
        assert "context.rewind_summary" in event_types


class TestCompactPartialSessionErrors:
    @staticmethod
    def test_raises_on_invalid_turn_index(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        records = [
            _make_user_record(1, "hello"),
            _make_assistant_record(1, "hi"),
        ]
        _write_history((tmp_path / "s4" / "history.jsonl"), records)

        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        with pytest.raises(ValueError, match="turn_index must be >= 1"):
            compact_partial_session(session_id="s4", turn_index=0, direction="from")

        with pytest.raises(ValueError, match="exceeds total turns"):
            compact_partial_session(session_id="s4", turn_index=99, direction="from")

    @staticmethod
    def test_raises_on_missing_history(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        with pytest.raises(ValueError, match="session history not found"):
            compact_partial_session(session_id="no_such", turn_index=1, direction="from")

    @staticmethod
    def test_raises_on_no_user_messages(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        records = [
            {
                "id": "a1",
                "role": "assistant",
                "content": "only assistant",
                "request_id": "r1",
                "timestamp": 1.0,
            },
        ]
        _write_history((tmp_path / "s5" / "history.jsonl"), records)

        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        with pytest.raises(ValueError, match="no user messages"):
            compact_partial_session(session_id="s5", turn_index=1, direction="from")

    @staticmethod
    def test_raises_on_unknown_direction(tmp_path, monkeypatch):
        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: tmp_path,
        )
        records = [
            _make_user_record(1, "hello"),
            _make_assistant_record(1, "hi"),
        ]
        _write_history((tmp_path / "s6" / "history.jsonl"), records)

        from jiuwenswarm.agents.harness.common.session_ops_service import compact_partial_session

        with pytest.raises(ValueError, match="unknown direction"):
            compact_partial_session(session_id="s6", turn_index=1, direction="bad")


# ---------------------------------------------------------------------------
# _build_messages_for_model  tests
# ---------------------------------------------------------------------------
class TestBuildMessagesForModel:
    @staticmethod
    def test_filters_system_and_tool_records():
        records = [
            {"role": "system", "content": "system prompt"},
            {"role": "user", "content": "user query"},
            {"role": "assistant", "content": "assistant reply", "event_type": "chat.final"},
            {"role": "tool", "content": '{"result": "ok"}'},
        ]
        result = _build_model_messages(records)

        assert len(result) == 2
        assert result[0].content == "user query"
        assert result[1].content == "assistant reply"

    @staticmethod
    def test_skips_tool_call_event_types():
        records = [
            {"role": "user", "content": "do something"},
            {"role": "assistant", "content": "calling tool", "event_type": "chat.tool_call"},
            {"role": "assistant", "content": "final answer", "event_type": "chat.final"},
        ]
        result = _build_model_messages(records)

        assert len(result) == 2
        assert result[0].content == "do something"
        assert result[1].content == "final answer"

    @staticmethod
    def test_strips_file_content_blocks():
        records = [
            {
                "role": "user",
                "content": "Look at this file\n<file-content path='x.py'>code here</file-content>\nWhat do you think?",
            },
        ]
        result = _build_model_messages(records)

        assert len(result) == 1
        assert "<file-content" not in result[0].content
        assert "Look at this file" in result[0].content
        assert "What do you think?" in result[0].content

    @staticmethod
    def test_skips_empty_content():
        records = [
            {"role": "user", "content": ""},
            {"role": "user", "content": "  "},
            {"role": "user", "content": "actual message"},
        ]
        result = _build_model_messages(records)

        assert len(result) == 1
        assert result[0].content == "actual message"

    @staticmethod
    def test_includes_compact_and_rewind_summary_events():
        records = [
            {"role": "user", "content": "start"},
            {"role": "assistant", "content": "compact summary", "event_type": "context.compact_summary"},
            {"role": "assistant", "content": "rewind summary", "event_type": "context.rewind_summary"},
        ]
        result = _build_model_messages(records)

        assert len(result) == 3
        assert result[1].content == "compact summary"
        assert result[2].content == "rewind summary"

    @staticmethod
    def test_skips_compact_boundary_events():
        records = [
            {"role": "user", "content": "before compact"},
            {"role": "assistant", "content": "Conversation compacted", "event_type": "context.compact_boundary"},
            {"role": "user", "content": "after compact"},
        ]
        result = _build_model_messages(records)

        assert len(result) == 2
        contents = [m.content for m in result]
        assert "Conversation compacted" not in contents

    @staticmethod
    def test_skips_non_dict_records():
        records = [
            "not a dict",
            {"role": "user", "content": "valid message"},
        ]
        result = _build_model_messages(records)

        assert len(result) == 1
        assert result[0].content == "valid message"


def _build_model_messages(records):
    from openjiuwen.core.foundation.llm.schema.message import UserMessage, AssistantMessage

    messages = []
    for rec in records:
        if not isinstance(rec, dict):
            continue
        role = rec.get("role")
        content = rec.get("content")
        if not isinstance(content, str) or not content.strip():
            continue

        event_type = rec.get("event_type")
        if role == "user":
            import re
            cleaned = re.sub(r"<file-content[^>]*>.*?</file-content>", "", content, flags=re.DOTALL).strip()
            if cleaned:
                messages.append(UserMessage(content=cleaned))
        elif role == "assistant":
            if event_type in ("chat.final", "context.compact_summary", "context.rewind_summary") or not event_type:
                if event_type in ("context.compact_boundary",):
                    continue
                messages.append(AssistantMessage(content=content))

    return messages


# ---------------------------------------------------------------------------
# compact_partial_prompts  tests
# ---------------------------------------------------------------------------
class TestCompactPartialPrompts:
    @staticmethod
    def test_no_tools_preamble_contains_critical():
        from jiuwenswarm.server.runtime.agent_adapter.compact_partial_prompts import NO_TOOLS_PREAMBLE

        assert "CRITICAL" in NO_TOOLS_PREAMBLE
        assert "Do NOT call any tools" in NO_TOOLS_PREAMBLE
        assert "<analysis>" in NO_TOOLS_PREAMBLE

    @staticmethod
    def test_partial_compact_prompt_has_required_sections():
        from jiuwenswarm.server.runtime.agent_adapter.compact_partial_prompts import PARTIAL_COMPACT_PROMPT

        assert "Primary Request and Intent" in PARTIAL_COMPACT_PROMPT
        assert "Key Technical Concepts" in PARTIAL_COMPACT_PROMPT
        assert "Files and Code Sections" in PARTIAL_COMPACT_PROMPT
        assert "Errors and fixes" in PARTIAL_COMPACT_PROMPT
        assert "Problem Solving" in PARTIAL_COMPACT_PROMPT
        assert "All user messages" in PARTIAL_COMPACT_PROMPT
        assert "Pending Tasks" in PARTIAL_COMPACT_PROMPT
        assert "Current Work" in PARTIAL_COMPACT_PROMPT
        assert "Optional Next Step" in PARTIAL_COMPACT_PROMPT

    @staticmethod
    def test_partial_compact_up_to_prompt_has_required_sections():
        from jiuwenswarm.server.runtime.agent_adapter.compact_partial_prompts import PARTIAL_COMPACT_UP_TO_PROMPT

        assert "Primary Request and Intent" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Key Technical Concepts" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Files and Code Sections" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Errors and fixes" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Problem Solving" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "All user messages" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Pending Tasks" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Work Completed" in PARTIAL_COMPACT_UP_TO_PROMPT
        assert "Context for Continuing Work" in PARTIAL_COMPACT_UP_TO_PROMPT

    @staticmethod
    def test_prompt_strings_are_not_empty():
        from jiuwenswarm.server.runtime.agent_adapter.compact_partial_prompts import (
            NO_TOOLS_PREAMBLE,
            PARTIAL_COMPACT_PROMPT,
            PARTIAL_COMPACT_UP_TO_PROMPT,
        )
        assert len(NO_TOOLS_PREAMBLE.strip()) > 0
        assert len(PARTIAL_COMPACT_PROMPT.strip()) > 0
        assert len(PARTIAL_COMPACT_UP_TO_PROMPT.strip()) > 0


# ---------------------------------------------------------------------------
# rewind_session_context with compact_summary / rewind_summary injection
# ---------------------------------------------------------------------------
class TestRewindSessionContextInjectsSummaries:
    @pytest.mark.asyncio
    async def test_compact_summary_injected_as_user_message(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        history = [
            {
                "role": "user", "id": "u1", "request_id": "r1",
                "content": "first question", "timestamp": 1.0,
            },
            {
                "role": "assistant", "content": "first answer",
                "event_type": "chat.final", "timestamp": 1.5,
            },
            {
                "role": "assistant", "content": "Conversation compacted",
                "event_type": "context.compact_boundary", "timestamp": 2.0,
            },
            {
                "role": "assistant",
                "content": "Summarized 2 messages from this point.",
                "event_type": "context.rewind_summary", "timestamp": 2.001,
            },
            {
                "role": "assistant",
                "content": "LLM summary: user asked a question and got an answer.",
                "event_type": "context.compact_summary", "timestamp": 2.002,
            },
        ]
        _write_history((sessions_dir / "s1" / "history.jsonl"), history)

        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: sessions_dir,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: sessions_dir,
        )

        from unittest.mock import AsyncMock, MagicMock

        mock_context_engine = MagicMock()
        mock_context_engine.get_context.return_value = None
        mock_context_engine.clear_context = AsyncMock()
        mock_context_engine.create_context = AsyncMock()
        mock_context_engine.save_contexts = AsyncMock()

        mock_react_agent = MagicMock()
        mock_react_agent.context_engine = mock_context_engine

        mock_session = MagicMock()
        mock_session.pre_run = AsyncMock()
        mock_session.post_run = AsyncMock()
        mock_session.update_state = MagicMock()

        mock_deep_agent = MagicMock()
        mock_deep_agent.react_agent = mock_react_agent
        mock_deep_agent.card = MagicMock()
        mock_deep_agent.save_state = MagicMock()

        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            return_value=mock_session,
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

            result = await rewind_session_context(
                deep_agent=mock_deep_agent,
                session_id="s1",
                turn_index=1,
            )

        assert result is True
        mock_context_engine.create_context.assert_called_once()
        _, kwargs = mock_context_engine.create_context.call_args
        history_messages = kwargs["history_messages"]
        from openjiuwen.core.foundation.llm.schema.message import UserMessage
        user_contents = [m.content for m in history_messages if isinstance(m, UserMessage)]
        assert "LLM summary: user asked a question and got an answer." in user_contents
        assert "Summarized 2 messages from this point." in user_contents

    @pytest.mark.asyncio
    async def test_rewind_summary_without_compact_summary(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        history = [
            {
                "role": "user", "id": "u1", "request_id": "r1",
                "content": "hello world", "timestamp": 1.0,
            },
            {
                "role": "assistant", "content": "hi there",
                "event_type": "chat.final", "timestamp": 1.5,
            },
            {
                "role": "assistant",
                "content": "Summarized 2 messages up to this point.",
                "event_type": "context.rewind_summary", "timestamp": 2.0,
            },
        ]
        _write_history((sessions_dir / "s2" / "history.jsonl"), history)

        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: sessions_dir,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: sessions_dir,
        )

        from unittest.mock import AsyncMock, MagicMock

        mock_context_engine = MagicMock()
        mock_context_engine.get_context.return_value = None
        mock_context_engine.clear_context = AsyncMock()
        mock_context_engine.create_context = AsyncMock()
        mock_context_engine.save_contexts = AsyncMock()

        mock_react_agent = MagicMock()
        mock_react_agent.context_engine = mock_context_engine

        mock_session = MagicMock()
        mock_session.pre_run = AsyncMock()
        mock_session.post_run = AsyncMock()
        mock_session.update_state = MagicMock()

        mock_deep_agent = MagicMock()
        mock_deep_agent.react_agent = mock_react_agent
        mock_deep_agent.card = MagicMock()
        mock_deep_agent.save_state = MagicMock()

        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            return_value=mock_session,
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

            result = await rewind_session_context(
                deep_agent=mock_deep_agent,
                session_id="s2",
                turn_index=1,
            )

        assert result is True
        mock_context_engine.create_context.assert_called_once()
        _, kwargs = mock_context_engine.create_context.call_args
        history_messages = kwargs["history_messages"]
        from openjiuwen.core.foundation.llm.schema.message import UserMessage
        user_contents = [m.content for m in history_messages if isinstance(m, UserMessage)]
        assert "Summarized 2 messages up to this point." in user_contents


class TestRewindSessionContextLiveSession:
    @pytest.mark.asyncio
    async def test_reuses_interaction_session_and_commits(self, tmp_path, monkeypatch):
        """Live DeepAgent session must be updated in-place (commit, not post_run)."""
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()

        history = [
            {
                "role": "user", "id": "u1", "request_id": "r1",
                "content": "10以内最大的质数", "timestamp": 1.0,
            },
            {
                "role": "assistant", "content": "7",
                "event_type": "chat.final", "timestamp": 1.5,
            },
        ]
        _write_history((sessions_dir / "live1" / "history.jsonl"), history)

        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: sessions_dir,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: sessions_dir,
        )

        from unittest.mock import AsyncMock, MagicMock

        mock_context_engine = MagicMock()
        mock_context_engine.get_context.return_value = None
        mock_context_engine.clear_context = AsyncMock()
        mock_context_engine.create_context = AsyncMock()
        mock_context_engine.save_contexts = AsyncMock()

        mock_react_agent = MagicMock()
        mock_react_agent.context_engine = mock_context_engine

        live_session = MagicMock()
        live_session.get_session_id.return_value = "live1"
        live_session.pre_run = AsyncMock()
        live_session.post_run = AsyncMock()
        live_session.commit = AsyncMock()
        live_session.update_state = MagicMock()

        mock_deep_agent = MagicMock()
        mock_deep_agent.react_agent = mock_react_agent
        mock_deep_agent.card = MagicMock()
        mock_deep_agent.save_state = MagicMock()
        mock_deep_agent._interaction_session = live_session
        mock_deep_agent._loop_session = live_session

        create_session = MagicMock()
        with patch(
            "openjiuwen.core.single_agent.create_agent_session",
            create_session,
        ):
            from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

            result = await rewind_session_context(
                deep_agent=mock_deep_agent,
                session_id="live1",
                turn_index=2,
            )

        assert result is True
        create_session.assert_not_called()
        live_session.pre_run.assert_not_called()
        live_session.post_run.assert_not_called()
        live_session.commit.assert_awaited()
        mock_context_engine.create_context.assert_called_once()
        _, kwargs = mock_context_engine.create_context.call_args
        assert kwargs["session"] is live_session
        history_messages = kwargs["history_messages"]
        from openjiuwen.core.foundation.llm.schema.message import UserMessage, AssistantMessage
        assert any(isinstance(m, UserMessage) and "质数" in m.content for m in history_messages)
        assert any(isinstance(m, AssistantMessage) and m.content == "7" for m in history_messages)

    @pytest.mark.asyncio
    async def test_empty_history_still_clears_live_context(self, tmp_path, monkeypatch):
        sessions_dir = tmp_path / "sessions"
        sessions_dir.mkdir()
        (sessions_dir / "empty1").mkdir()
        (sessions_dir / "empty1" / "history.jsonl").write_text("", encoding="utf-8")

        monkeypatch.setattr(
            "jiuwenswarm.agents.harness.common.session_ops_service.get_agent_sessions_dir",
            lambda: sessions_dir,
        )
        monkeypatch.setattr(
            "jiuwenswarm.server.runtime.session.session_history.get_agent_sessions_dir",
            lambda: sessions_dir,
        )

        from unittest.mock import AsyncMock, MagicMock

        mock_context_engine = MagicMock()
        mock_context_engine.get_context.return_value = None
        mock_context_engine.clear_context = AsyncMock()
        mock_context_engine.create_context = AsyncMock()
        mock_context_engine.save_contexts = AsyncMock()

        mock_react_agent = MagicMock()
        mock_react_agent.context_engine = mock_context_engine

        live_session = MagicMock()
        live_session.get_session_id.return_value = "empty1"
        live_session.commit = AsyncMock()
        live_session.post_run = AsyncMock()
        live_session.update_state = MagicMock()

        mock_deep_agent = MagicMock()
        mock_deep_agent.react_agent = mock_react_agent
        mock_deep_agent.card = MagicMock()
        mock_deep_agent.save_state = MagicMock()
        mock_deep_agent._interaction_session = live_session

        from jiuwenswarm.agents.harness.common.session_ops_service import rewind_session_context

        result = await rewind_session_context(
            deep_agent=mock_deep_agent,
            session_id="empty1",
            turn_index=1,
        )

        assert result is True
        mock_context_engine.clear_context.assert_awaited()
        mock_context_engine.create_context.assert_called_once()
        _, kwargs = mock_context_engine.create_context.call_args
        assert kwargs["history_messages"] == []
        live_session.commit.assert_awaited()
        live_session.post_run.assert_not_called()
