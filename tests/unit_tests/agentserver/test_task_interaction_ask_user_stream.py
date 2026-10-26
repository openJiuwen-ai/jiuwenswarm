# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""裸 ``task_interaction`` controller_output → 前端提问卡 的解析测试。

背景:原生 harness 的 ask_user 中断以裸 task_interaction controller_output
到达解析层,stream_utils 此前仅识别 ``__interaction__`` 载荷,该 chunk 被静默
丢弃——前端既收不到提问卡也收不到 chat.final。现需识别嵌入的 ask_user
中断值并转换为 ``chat.ask_user_question``,无法解析时下发兜底输入卡。
"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.agent_adapter.interface_deep import (
    JiuWenSwarmDeepAdapter,
)
from jiuwenswarm.server.utils import stream_utils
from jiuwenswarm.server.utils.stream_utils import parse_stream_chunk


def _ask_user_value(**overrides) -> dict:
    value = {
        "tool_name": "ask_user",
        "tool_call_id": "call_ask_user_1",
        "questions": [
            {
                "question": "文件存放到哪个目录?",
                "header": "Location",
                "options": [],
                "multi_select": False,
            }
        ],
    }
    value.update(overrides)
    return value


def _task_interaction_chunk(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(type="controller_output", payload=payload)


def test_parse_task_interaction_ask_user_card_from_bare_controller_output() -> None:
    """裸 task_interaction(无 __interaction__ 标记)必须转出提问卡,不再被丢弃。"""
    chunk = _task_interaction_chunk(
        {"type": "task_interaction", "data": [_ask_user_value()]}
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "call_ask_user_1"
    assert parsed["source"] == "ask_user_interrupt"
    assert parsed["questions"][0]["question"] == "文件存放到哪个目录?"


def test_parse_task_interaction_ask_user_card_appends_other_option() -> None:
    """带选项的问题转换为卡片时追加 __other__ 自定义输入项。"""
    value = _ask_user_value(
        questions=[
            {
                "question": "选择目录",
                "options": [
                    {"label": "当前项目目录", "description": ""},
                    {"label": "桌面", "description": ""},
                ],
            }
        ]
    )
    chunk = _task_interaction_chunk({"type": "task_interaction", "data": [value]})

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    options = parsed["questions"][0]["options"]
    assert len(options) == 3
    assert options[-1]["label"] == "Other"


def test_parse_task_interaction_from_pydantic_model_dump() -> None:
    """ToolCallInterruptRequest 等模型对象经 model_dump 展开后仍可识别。"""

    class _FakeInterruptRequest:
        def __init__(self, dump: dict) -> None:
            self._dump = dump

        def model_dump(self, mode: str = "python") -> dict:
            return dict(self._dump)

    chunk = _task_interaction_chunk(
        {
            "type": "task_interaction",
            "result": _FakeInterruptRequest(
                _ask_user_value(tool_call_id="call_dump_1")
            ),
        }
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "call_dump_1"


def test_parse_task_interaction_questions_in_tool_args_json_string() -> None:
    """questions 藏在 tool_args JSON 字符串(StructuredAskUserRail 形态)也可解析。"""
    value = {
        "tool_name": "ask_user",
        "tool_call_id": "call_json",
        "tool_args": '{"questions": [{"question": "目录?"}]}',
    }
    chunk = _task_interaction_chunk({"type": "task_interaction", "data": [value]})

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["request_id"] == "call_json"
    assert parsed["questions"][0]["question"] == "目录?"


def test_parse_task_interaction_prefers_structured_questions_over_plain() -> None:
    """结构化 questions 候选优先于 plain query,即使 plain 排在前面。"""
    plain = {
        "tool_name": "ask_user",
        "tool_call_id": "call_plain",
        "message": "直接回答问题",
    }
    structured = _ask_user_value(tool_call_id="call_struct")
    chunk = _task_interaction_chunk(
        {"type": "task_interaction", "data": [plain, structured]}
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["request_id"] == "call_struct"
    assert parsed["questions"][0]["question"] == "文件存放到哪个目录?"


def test_parse_task_interaction_request_id_prefers_value_over_candidates() -> None:
    """request_id 取中断值自身的 tool_call_id,而非外层收集到的候选。"""
    payload = {
        "type": "task_interaction",
        "tool_call_id": "outer_candidate",
        "data": [_ask_user_value(tool_call_id="inner_call")],
    }

    parsed = stream_utils.parse_task_interaction_payload(payload)

    assert parsed is not None
    assert parsed["request_id"] == "inner_call"


def test_parse_task_interaction_request_id_falls_back_to_collected_candidates() -> None:
    """中断值无 id 时回退到外层收集的候选(如 data 兄弟节点的 tool_call_id)。"""
    value = {
        k: v for k, v in _ask_user_value().items() if k != "tool_call_id"
    }
    payload = {
        "type": "task_interaction",
        "data": [value, {"tool_call_id": "sibling_call"}],
    }

    parsed = stream_utils.parse_task_interaction_payload(payload)

    assert parsed is not None
    assert parsed["request_id"] == "sibling_call"


def test_parse_task_interaction_without_ask_user_emits_fallback_card() -> None:
    """无法解析出 ask_user 值时必须下发兜底输入卡,前端不能没有输入入口。"""
    chunk = _task_interaction_chunk(
        {"type": "task_interaction", "data": [{"tool_call_id": "call_x", "status": "pending"}]}
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "call_x"
    assert parsed["source"] == "task_interaction_fallback"
    assert parsed["questions"][0]["question"]
    assert parsed["questions"][0]["options"] == []
    assert parsed["questions"][0]["multi_select"] is False


def test_fallback_card_generates_request_id_when_missing() -> None:
    card = stream_utils._fallback_awaiting_input_card("")
    assert card["request_id"].startswith("task_interaction_")

    card = stream_utils._fallback_awaiting_input_card("keep_me")
    assert card["request_id"] == "keep_me"


def test_parse_task_interaction_falls_through_unconverted_interaction_payloads() -> None:
    """__interaction__ 载荷存在但不可转换时,不得静默丢弃——落到
    task_interaction 分支继续解析嵌入的 ask_user 值。"""
    chunk = _task_interaction_chunk(
        {
            "type": "task_interaction",
            "data": [
                {"type": "__interaction__", "payload": _ask_user_value()}
            ],
        }
    )

    parsed = parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "call_ask_user_1"


def test_collect_request_id_candidates_gathers_ids_from_shapes() -> None:
    payload = {
        "tool_call_id": "outer_call",
        "interrupt_ids": ["int_1", "int_2"],
        "data": [
            {"request_id": "inner_req"},
            {"interrupt_id": "inner_int"},
        ],
    }

    ids = stream_utils._collect_request_id_candidates(payload)

    assert ids == ["outer_call", "int_1", "int_2", "inner_req", "inner_int"]


def test_iter_ask_user_interrupt_values_terminates_on_cycles() -> None:
    cyclic: list = []
    cyclic.append(cyclic)
    container = {"tool_name": "ask_user", "questions": [], "nested": cyclic}

    values = list(stream_utils._iter_ask_user_interrupt_values(container))

    assert values == [container]


def test_interface_deep_parse_stream_chunk_builds_task_interaction_card() -> None:
    """interface_deep 镜像解析:task_interaction 同样转出提问卡。"""
    chunk = _task_interaction_chunk(
        {"type": "task_interaction", "data": [_ask_user_value()]}
    )

    parsed = JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk)

    assert parsed is not None
    assert parsed["event_type"] == "chat.ask_user_question"
    assert parsed["request_id"] == "call_ask_user_1"


def test_interface_deep_parse_stream_chunk_unparsable_task_interaction_drops() -> None:
    """interface_deep 镜像解析:不可解析的 task_interaction 归为控制面元数据
    (返回 None),兜底卡由外层 stream_utils 负责。"""
    chunk = _task_interaction_chunk(
        {"type": "task_interaction", "data": [{"status": "pending"}]}
    )

    assert JiuWenSwarmDeepAdapter._parse_stream_chunk(chunk) is None
