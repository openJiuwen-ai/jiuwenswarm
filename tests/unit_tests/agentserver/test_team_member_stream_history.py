# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team 流里成员输出必须以 teammate 身份落盘，且不污染 leader 的 final/reasoning。

复现场景（祝福语团队会话）：leader 的 reasoning 与成员 r2-writer-1 的 delta 交错
到达，成员的 tool_call 把共用缓冲冲成一条 ``role=assistant`` chat.final（正文是
成员的、reasoning 是 leader 的、没有 member_name）；成员最后一段正文又被 leader
的 tool_call 冲成 assistant 记录，与成员自己的 chat.final 重复。
"""

from __future__ import annotations

from typing import Any, AsyncIterator, List

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest, AgentResponseChunk
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module
from jiuwenswarm.server.runtime.agent_adapter.interface import JiuWenSwarm
from jiuwenswarm.server.runtime.agent_adapter.team_member_stream_history import (
    TeamMemberStreamHistory,
    team_member_stream_key,
)

_TEAM = {"team_id": "team_x", "team_name": "team_x", "rid": "round-1"}
_LEADER = {"role": "leader", **_TEAM}
_WRITER = {"role": "teammate", "member_name": "r2-writer-1", **_TEAM}


class _ScriptedAdapter:
    """把预设 payload 列表当作 adapter 输出流回放。"""

    def __init__(self, payloads: list[dict[str, Any]], *, as_dict: bool) -> None:
        self._payloads = payloads
        self._as_dict = as_dict

    async def create_instance(self, config: dict[str, Any] | None = None) -> None:
        return None

    async def reload_agent_config(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def process_message_impl(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def process_message_stream_impl(
        self,
        request: AgentRequest,
        _inputs: dict[str, Any],
    ) -> AsyncIterator[Any]:
        for payload in self._payloads:
            if self._as_dict:
                yield dict(payload)
            else:
                yield AgentResponseChunk(
                    request_id=request.request_id,
                    channel_id=request.channel_id,
                    payload=dict(payload),
                    is_complete=False,
                )

    async def process_interrupt(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def reconcile_session_mcp(self, *_args: Any, **_kwargs: Any) -> None:
        return None

    async def handle_user_answer(self, *_args: Any, **_kwargs: Any) -> Any:
        return None

    async def handle_heartbeat(self, *_args: Any, **_kwargs: Any) -> Any:
        return None


async def _run_team_stream(
    monkeypatch: pytest.MonkeyPatch,
    payloads: list[dict[str, Any]],
    *,
    as_dict: bool = False,
) -> List[dict[str, Any]]:
    facade = JiuWenSwarm()
    recorded: List[dict[str, Any]] = []
    monkeypatch.setattr(facade, "_adapter", _ScriptedAdapter(payloads, as_dict=as_dict))
    monkeypatch.setattr(facade, "_sdk_name", "harness")
    monkeypatch.setattr(
        interface_module,
        "append_history_record",
        lambda **kwargs: recorded.append(kwargs),
    )
    monkeypatch.setattr(interface_module, "get_config", lambda: {"preferred_language": "zh"})
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _cfg: "off")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda q, **_kw: q)

    request = AgentRequest(
        request_id="req-team-member-history",
        channel_id="web",
        session_id="team_member_history_sess",
        params={"query": "写祝福语", "mode": "team", "team": True},
    )
    async for _chunk in facade.process_message_stream(request):
        pass
    return [r for r in recorded if r.get("role") == "assistant"]


def _finals(records: List[dict[str, Any]]) -> List[dict[str, Any]]:
    return [
        r for r in records
        if r.get("event_type") == "chat.final" and str(r.get("content") or "").strip()
    ]


def _extra(record: dict[str, Any]) -> dict[str, Any]:
    return record.get("extra") or {}


_INTERLEAVED_STREAM: list[dict[str, Any]] = [
    # leader 思考 + 派活
    {"event_type": "chat.reasoning", "content": "leader 在想怎么派活", **_LEADER},
    {"event_type": "chat.delta", "content": "现在派给 r2-writer-1。", **_LEADER},
    {"event_type": "chat.tool_call", "tool_call": {"name": "create_task"}, **_LEADER},
    # leader 新一轮思考与成员输出交错
    {"event_type": "chat.reasoning", "content": "我打错了 update_task 的参数", **_LEADER},
    {"event_type": "chat.delta", "content": "收到新任务。", **_WRITER},
    {"event_type": "chat.delta", "content": "先读取前两轮交付文件。", **_WRITER},
    {"event_type": "chat.tool_call", "tool_call": {"name": "read_file"}, **_WRITER},
    {"event_type": "chat.tool_call", "tool_call": {"name": "update_task"}, **_LEADER},
    {"event_type": "chat.delta", "content": "已派活并唤醒。", **_LEADER},
    {"event_type": "chat.final", "content": "已派活并唤醒。", **_LEADER},
    # 成员最后一段：leader 的 tool_call 夹在中间，不能把成员正文冲成 assistant
    {"event_type": "chat.delta", "content": "祝福短句已完成交付。", **_WRITER},
    {"event_type": "chat.tool_call", "tool_call": {"name": "view_task"}, **_LEADER},
    {"event_type": "chat.final", "content": "祝福短句已完成交付。", **_WRITER},
]


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
async def test_member_output_is_persisted_as_teammate_once(
    monkeypatch: pytest.MonkeyPatch,
    as_dict: bool,
) -> None:
    records = await _run_team_stream(monkeypatch, _INTERLEAVED_STREAM, as_dict=as_dict)
    finals = _finals(records)

    member_finals = [r for r in finals if _extra(r).get("role") == "teammate"]
    leader_finals = [r for r in finals if _extra(r).get("role") != "teammate"]

    assert [r["content"] for r in member_finals] == [
        "收到新任务。先读取前两轮交付文件。",
        "祝福短句已完成交付。",
    ]
    for record in member_finals:
        extra = _extra(record)
        assert extra["member_name"] == "r2-writer-1"
        assert extra["team_id"] == "team_x"
        assert extra["rid"] == "round-1"
        # 成员不带 leader 的思考
        assert "reasoning_content" not in extra

    assert [r["content"] for r in leader_finals] == ["现在派给 r2-writer-1。", "已派活并唤醒。"]
    assert _extra(leader_finals[0])["reasoning_content"] == "leader 在想怎么派活"


@pytest.mark.asyncio
async def test_leader_reasoning_stays_with_leader_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_team_stream(monkeypatch, _INTERLEAVED_STREAM)

    tool_calls = [r for r in records if r.get("event_type") == "chat.tool_call"]
    update_task = next(
        r for r in tool_calls if _extra(r).get("tool_call", {}).get("name") == "update_task"
    )
    read_file = next(
        r for r in tool_calls if _extra(r).get("tool_call", {}).get("name") == "read_file"
    )
    assert _extra(update_task)["reasoning_content"] == "我打错了 update_task 的参数"
    assert "reasoning_content" not in _extra(read_file)


@pytest.mark.asyncio
async def test_member_empty_final_flushes_member_segment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    records = await _run_team_stream(
        monkeypatch,
        [
            {"event_type": "chat.delta", "content": "成员正文", **_WRITER},
            {"event_type": "chat.delta", "content": "leader 正文", **_LEADER},
            {"event_type": "chat.final", "content": "", **_WRITER},
            {"event_type": "chat.final", "content": "", **_LEADER},
        ],
    )

    finals = _finals(records)
    assert [(r["content"], _extra(r).get("role")) for r in finals] == [
        ("成员正文", "teammate"),
        ("leader 正文", None),
    ]


def test_team_member_stream_key() -> None:
    assert team_member_stream_key(_WRITER) == "r2-writer-1"
    assert team_member_stream_key({"role": "teammate"}) == "teammate"
    assert team_member_stream_key(_LEADER) is None
    assert team_member_stream_key({"event_type": "chat.delta"}) is None
    assert team_member_stream_key(None) is None


def test_member_buckets_are_independent() -> None:
    history = TeamMemberStreamHistory()
    other = {**_WRITER, "member_name": "r2-writer-2"}
    history.note_delta("r2-writer-1", "甲", _WRITER)
    history.note_delta("r2-writer-2", "乙", other)
    history.note_delta("r2-writer-1", "丙", _WRITER)

    first = history.take_pending_final("r2-writer-1")
    assert first is not None
    assert first.content == "甲丙"
    assert first.extra["member_name"] == "r2-writer-1"

    rest = history.take_all_pending_finals()
    assert [(item.content, item.extra["member_name"]) for item in rest] == [("乙", "r2-writer-2")]


def test_non_empty_member_final_supersedes_pending_text() -> None:
    history = TeamMemberStreamHistory()
    history.note_delta("r2-writer-1", "最终段", _WRITER)
    history.discard_text("r2-writer-1", final_content="最终段")
    assert history.take_pending_final("r2-writer-1") is None


@pytest.mark.asyncio
@pytest.mark.parametrize("as_dict", [False, True])
@pytest.mark.parametrize("with_text", [False, True])
@pytest.mark.parametrize("boundary", [
    "chat.ask_user_question", "harness.activate_interaction", "plan.approval_required",
])
async def test_member_interaction_preserves_its_reasoning_and_text(
    monkeypatch: pytest.MonkeyPatch,
    as_dict: bool,
    with_text: bool,
    boundary: str,
) -> None:
    payloads = [
        {"event_type": "chat.reasoning", "content": "leader 思考", **_LEADER},
        {"event_type": "chat.reasoning", "content": "成员需要确认", **_WRITER},
    ]
    if with_text:
        payloads.append({"event_type": "chat.delta", "content": "成员正文", **_WRITER})
    payloads.extend([
        {"event_type": boundary, "request_id": "confirm-1", **_WRITER},
        {"event_type": "chat.final", "content": "leader 正文", **_LEADER},
    ])

    records = await _run_team_stream(monkeypatch, payloads, as_dict=as_dict)

    reasoning_records = [r for r in records if _extra(r).get("reasoning_content")]
    assert [_extra(r)["reasoning_content"] for r in reasoning_records] == [
        "成员需要确认", "leader 思考",
    ]
    member_record = reasoning_records[0]
    assert member_record["event_type"] == ("chat.final" if with_text else "chat.reasoning")
    assert member_record["content"] == ("成员正文" if with_text else "")
    assert _extra(member_record)["member_name"] == _WRITER["member_name"]
    assert _extra(member_record)["team_id"] == _WRITER["team_id"]
    assert _extra(member_record)["role"] == "teammate"
