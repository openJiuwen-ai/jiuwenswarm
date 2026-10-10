# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import patch

from openjiuwen.harness.subagent_runtime import (
    SUBAGENT_ACTIVITY_EVENT_TYPE,
    SUBAGENT_UPDATED_EVENT_TYPE,
)
from openjiuwen.harness.tools.subagent.subagent_tools import build_subagent_tools

from jiuwenswarm.agents.harness.common.rails.browser_task_prompt_rail import (
    BrowserTaskPromptRail,
)
from jiuwenswarm.common.config import is_subagent_runtime_enabled
from jiuwenswarm.server.runtime.agent_adapter import interface_deep as interface_deep_module
from jiuwenswarm.server.runtime.agent_adapter.interface_deep import JiuWenSwarmDeepAdapter


class TestSubagentRuntimeConfig:
    @staticmethod
    def test_disabled_by_default() -> None:
        assert is_subagent_runtime_enabled({"react": {}}) is False

    @staticmethod
    def test_enabled_when_configured() -> None:
        config = {"react": {"subagent_runtime": {"enabled": True}}}
        assert is_subagent_runtime_enabled(config) is True

    @staticmethod
    def test_runtime_flag_follows_config() -> None:
        enabled_rail = BrowserTaskPromptRail(
            enable_subagent_runtime=is_subagent_runtime_enabled(
                {"react": {"subagent_runtime": {"enabled": True}}},
            ),
        )
        assert enabled_rail.enable_subagent_runtime is True

        disabled_rail = BrowserTaskPromptRail(
            enable_subagent_runtime=is_subagent_runtime_enabled(
                {"react": {"subagent_runtime": {"enabled": False}}},
            ),
        )
        assert disabled_rail.enable_subagent_runtime is False


def test_sdk_runtime_tool_set_is_exact_and_unique() -> None:
    tools = build_subagent_tools(SimpleNamespace())
    names = [tool.card.name for tool in tools]

    assert names == [
        "subagent_spawn",
        "subagent_wait",
        "subagent_list",
        "subagent_send_input",
        "subagent_close",
        "subagent_resume",
    ]
    assert len(names) == len(set(names))


def _map_subagent_updated_chunk(chunk_type: str, payload: dict) -> dict | None:
    if chunk_type != SUBAGENT_UPDATED_EVENT_TYPE:
        return None
    projection = payload.get("subagent_updated")
    if not isinstance(projection, dict):
        return None
    return JiuWenSwarmDeepAdapter.project_subagent_updated_for_web(projection)


class TestSubagentStreamMapping:
    @staticmethod
    def setup_method() -> None:
        JiuWenSwarmDeepAdapter.clear_subagent_progress_batches()

    @staticmethod
    def test_maps_subagent_updated_to_chat_subtask_update() -> None:
        projection = {
            "subagent_id": "sess_sub_general_abc123",
            "parent_session_id": "parent-sess-1",
            "status": "running",
            "display_name": "Researcher",
            "task_description": "find login code",
        }
        parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {"subagent_updated": projection},
        )
        assert parsed is not None
        assert parsed["event_type"] == "chat.subtask_update"
        assert parsed["subagent_id"] == projection["subagent_id"]
        assert parsed["task_id"] == projection["subagent_id"]
        assert parsed["description"] == "Researcher"
        assert parsed["status"] == "running"
        assert parsed["legacy_status"] == "starting"
        assert parsed["index"] == 0
        assert parsed["total"] == 1
        assert parsed["is_parallel"] is False

    @staticmethod
    def test_parallel_subagents_get_distinct_indexes() -> None:
        parent_session_id = "parent-sess-parallel"
        first = {
            "subagent_id": "sub-a",
            "parent_session_id": parent_session_id,
            "status": "running",
            "display_name": "general-purpose",
        }
        second = {
            "subagent_id": "sub-b",
            "parent_session_id": parent_session_id,
            "status": "running",
            "display_name": "explore",
        }
        first_parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {"subagent_updated": first},
        )
        second_parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {"subagent_updated": second},
        )
        assert first_parsed is not None
        assert second_parsed is not None
        assert first_parsed["index"] == 0
        assert second_parsed["index"] == 1
        assert first_parsed["total"] == 1
        assert second_parsed["total"] == 2
        assert first_parsed["is_parallel"] is False
        assert second_parsed["is_parallel"] is True

    @staticmethod
    def test_maps_idle_completed_to_legacy_completed() -> None:
        parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {
                "subagent_updated": {
                    "subagent_id": "sid-1",
                    "parent_session_id": "parent-sess-idle",
                    "status": "idle",
                    "turn_outcome": "completed",
                    "lifecycle": "live",
                    "can_send_input": True,
                    "display_name": "Explorer",
                }
            },
        )
        assert parsed is not None
        assert parsed["status"] == "idle"
        assert parsed["turn_outcome"] == "completed"
        assert parsed["legacy_status"] == "completed"
        assert parsed["can_send_input"] is True

    @staticmethod
    def test_maps_idle_failed_preserves_error_and_legacy() -> None:
        parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {
                "subagent_updated": {
                    "subagent_id": "sid-1",
                    "status": "idle",
                    "turn_outcome": "failed",
                    "error": {"code": "TIMEOUT", "message": "turn timeout"},
                    "display_name": "Explorer",
                }
            },
        )
        assert parsed is not None
        assert parsed["status"] == "idle"
        assert parsed["legacy_status"] == "error"
        assert parsed["error"] == {"code": "TIMEOUT", "message": "turn timeout"}
        assert parsed["message"] == "turn timeout"

    @staticmethod
    def test_maps_closed_completed_to_legacy_completed() -> None:
        parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {
                "subagent_updated": {
                    "subagent_id": "sid-1",
                    "status": "closed",
                    "closed_reason": "manual",
                    "lifecycle": "closed",
                    "needs_resume": True,
                    "display_name": "Explorer",
                }
            },
        )
        assert parsed is not None
        assert parsed["status"] == "closed"
        assert parsed["closed_reason"] == "manual"
        assert parsed["legacy_status"] == "completed"
        assert parsed["needs_resume"] is True

    @staticmethod
    def test_maps_closed_failed_to_legacy_error() -> None:
        parsed = _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {
                "subagent_updated": {
                    "subagent_id": "sid-1",
                    "status": "closed",
                    "closed_reason": "failed",
                    "error": {"code": "TIMEOUT", "message": "turn timeout"},
                }
            },
        )
        assert parsed is not None
        assert parsed["status"] == "closed"
        assert parsed["legacy_status"] == "error"
        assert parsed["message"] == "turn timeout"

    @staticmethod
    def test_invalid_subagent_updated_payload_is_skipped() -> None:
        assert _map_subagent_updated_chunk(
            SUBAGENT_UPDATED_EVENT_TYPE,
            {"subagent_updated": "bad"},
        ) is None

    @staticmethod
    def test_subagent_activity_is_persisted_for_subagent_history() -> None:
        projection = {
            "subagent_id": "sub-a",
            "task_id": "turn-1",
            "seq": 4,
            "kind": "tool_call",
            "summary": "search market data",
            "at_ms": 1787019579059,
            "phase_id": 2,
            "tool_name": "web_search",
            "tool_call_id": "call-4",
        }

        with patch.object(interface_deep_module, "append_history_record") as append_history:
            parsed = JiuWenSwarmDeepAdapter.parse_stream_chunk(
                SimpleNamespace(
                    type=SUBAGENT_ACTIVITY_EVENT_TYPE,
                    payload={"subagent_activity": projection},
                ),
                parent_session_id="parent-sess-activity",
            )

        assert parsed == {"event_type": "chat.subagent_activity", **projection}
        append_history.assert_called_once()
        assert append_history.call_args.kwargs["subagent_id"] == "sub-a"
        assert append_history.call_args.kwargs["session_id"] == "parent-sess-activity"
        assert append_history.call_args.kwargs["event_type"] == "chat.subagent_activity"
        assert append_history.call_args.kwargs["extra"]["subagent_activity"] == {
            **projection,
            "parent_session_id": "parent-sess-activity",
        }

    @staticmethod
    def test_subagent_roster_is_persisted_for_subagent_history() -> None:
        projection = {
            "subagent_id": "sub-a",
            "parent_session_id": "parent-sess-roster",
            "status": "idle",
            "lifecycle": "live",
            "turn_outcome": "completed",
            "updated_at": 1787019579059,
            "revision": 5,
        }
        web_payload = {
            "event_type": "chat.subtask_update",
            "subagent_id": "sub-a",
            "parent_session_id": "parent-sess-roster",
            "status": "idle",
        }

        with patch.object(interface_deep_module, "append_history_record") as append_history:
            JiuWenSwarmDeepAdapter.persist_subagent_roster_history(projection, web_payload)

        assert append_history.call_args.kwargs["session_id"] == "parent-sess-roster"
        assert append_history.call_args.kwargs["subagent_id"] == "sub-a"
        assert append_history.call_args.kwargs["event_type"] == "chat.subtask_update"

    @staticmethod
    def test_subagent_activity_without_seq_uses_stable_request_id() -> None:
        projection_without_seq = {
            "subagent_id": "sub-a",
            "task_id": "turn-1",
            "kind": "thinking",
            "summary": "without sequence",
            "at_ms": 1787019579060,
        }

        with patch.object(interface_deep_module, "append_history_record") as append_history:
            JiuWenSwarmDeepAdapter.persist_subagent_activity({
                **projection_without_seq,
                "parent_session_id": "parent-sess-activity",
            })

        request_id = append_history.call_args.kwargs["request_id"]
        assert request_id.startswith("sub-a:activity:turn-1:")
        assert not request_id.endswith(":None")

    @staticmethod
    def test_subagent_transcript_persists_parent_session_scope() -> None:
        projection = {
            "parent_session_id": "parent-sess-final",
            "subagent_id": "sub-a",
            "seq": 7,
            "event_type": "chat.final",
            "content": "final answer",
            "at_ms": 1787019579059,
        }

        with patch.object(interface_deep_module, "append_history_record") as append_history:
            JiuWenSwarmDeepAdapter.persist_subagent_transcript_message(projection)

        append_history.assert_called_once()
        assert append_history.call_args.kwargs["session_id"] == "parent-sess-final"
        assert append_history.call_args.kwargs["subagent_id"] == "sub-a"
        assert append_history.call_args.kwargs["event_type"] == "chat.final"
        assert append_history.call_args.kwargs["extra"]["parent_session_id"] == "parent-sess-final"
