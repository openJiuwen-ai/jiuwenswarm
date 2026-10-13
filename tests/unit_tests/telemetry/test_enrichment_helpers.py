from __future__ import annotations

import asyncio
import copy
import json
import threading
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import FrozenInstanceError
from types import SimpleNamespace
from typing import Any

import pytest

from openjiuwen.harness.tools.base_tool import ToolOutput

from jiuwenswarm.telemetry.enrichment import (
    ContextTokenBreakdown,
    SkillObservation,
    UsageBreakdown,
    classify_decision,
    count_context_tokens,
    extract_skill,
    extract_usage,
    message_content,
    message_role,
    serialize_input_messages,
    serialize_output_message,
    serialize_tool_definitions,
)


class _Opaque:
    __slots__ = ()


class _ExplodingString:
    def __str__(self) -> str:
        raise RuntimeError("must not escape telemetry helpers")


class _CountingCounter:
    def count(self, text: str) -> int:
        return len(text)


class _RecordingCounter:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def count(self, text: str) -> int:
        self.inputs.append(text)
        return len(text)


class _FailingCounter:
    def count(self, text: str) -> int:
        raise RuntimeError(text)


class _PartiallyFailingCounter:
    def __init__(self) -> None:
        self.inputs: list[str] = []

    def count(self, text: str) -> int:
        self.inputs.append(text)
        if "functions.weather" in text:
            raise RuntimeError(text)
        return len(text)


class _ProbeMapping(Mapping[str, Any]):
    def __init__(
        self,
        values: dict[str, Any] | None = None,
        *,
        get_errors: dict[str, BaseException] | None = None,
        iter_error: BaseException | None = None,
        items_error: BaseException | None = None,
    ) -> None:
        self._values = values or {}
        self._get_errors = get_errors or {}
        self._iter_error = iter_error
        self._items_error = items_error

    def __getitem__(self, key: str) -> Any:
        return self._values[key]

    def __iter__(self):
        if self._iter_error is not None:
            raise self._iter_error
        return iter(self._values)

    def __len__(self) -> int:
        return len(self._values)

    def get(self, key: str, default: Any = None) -> Any:
        error = self._get_errors.get(key) or self._get_errors.get("*")
        if error is not None:
            raise error
        return self._values.get(key, default)

    def items(self):
        if self._items_error is not None:
            raise self._items_error
        return super().items()


class _ExplodingIterable:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def __iter__(self):
        raise self._error


class _ExplodingList(list[Any]):
    def __init__(self, error: BaseException) -> None:
        super().__init__(["unreachable"])
        self._error = error

    def __iter__(self):
        raise self._error


class _ExplodingSet(set[Any]):
    def __init__(self, error: BaseException) -> None:
        super().__init__({"unreachable"})
        self._error = error

    def __iter__(self):
        raise self._error


class _SelfDump:
    def model_dump(self):
        return self


class _ControlDump:
    def __init__(self, error: BaseException) -> None:
        self._error = error

    def model_dump(self):
        raise self._error


def test_message_role_and_content_support_dict_and_object_shapes() -> None:
    assert message_role({"role": "user", "content": "dict"}) == "user"
    assert message_content({"role": "user", "content": "dict"}) == "dict"
    message = SimpleNamespace(role="assistant", content="object")
    assert message_role(message) == "assistant"
    assert message_content(message) == "object"
    assert message_role({"role": ""}) == "unknown"
    assert message_content({"content": None}) == ""


def test_input_serialization_is_stable_unicode_safe_and_json_compatible() -> None:
    messages = [
        {"content": "你好", "role": "user"},
        {
            "role": "tool",
            "tool_call_id": "call-1",
            "content": _Opaque(),
        },
    ]

    first = serialize_input_messages(messages, max_chars=10_000)
    second = serialize_input_messages(messages, max_chars=10_000)

    assert first == second
    assert "你好" in first
    assert "\\u4f60" not in first
    assert json.loads(first) == [
        {"parts": [{"content": "你好", "type": "text"}], "role": "user"},
        {
            "parts": [{"content": "<_Opaque>", "type": "text"}],
            "role": "tool",
            "tool_call_id": "call-1",
        },
    ]


def test_serialization_degrades_broken_mappings_stably() -> None:
    broken_items = _ProbeMapping(
        items_error=RuntimeError("items unavailable")
    )
    broken_iteration = _ProbeMapping(
        {"value": 1}, iter_error=RuntimeError("iteration unavailable")
    )
    first = serialize_input_messages(
        [{"role": "user", "content": broken_items}], max_chars=10_000
    )
    second = serialize_input_messages(
        [{"role": "user", "content": broken_items}], max_chars=10_000
    )
    iteration_payload = serialize_input_messages(
        [{"role": "user", "content": broken_iteration}], max_chars=10_000
    )
    broken_get = _ProbeMapping(get_errors={"*": RuntimeError("get unavailable")})
    broken_get_payload = json.loads(
        serialize_input_messages([broken_get], max_chars=10_000)
    )
    assert first == second
    assert "<_ProbeMapping>" in first
    assert "<_ProbeMapping>" in iteration_payload
    assert broken_get_payload == [
        {"parts": [{"content": "", "type": "text"}], "role": "unknown"}
    ]


def test_serialization_degrades_broken_iterables_stably() -> None:
    broken_iterable_payload = serialize_input_messages(
        _ExplodingIterable(RuntimeError("iteration unavailable")),
        max_chars=10_000,
    )

    assert "<_ExplodingIterable>" in broken_iterable_payload


def test_failed_tool_call_iteration_degrades_as_an_empty_collection() -> None:
    with_content = {
        "content": "answer",
        "tool_calls": _ExplodingIterable(RuntimeError("iteration unavailable")),
    }
    without_content = {
        "content": "",
        "tool_calls": _ExplodingIterable(RuntimeError("iteration unavailable")),
    }

    assert classify_decision(with_content) == ("answer", [])
    assert classify_decision(without_content) == ("unknown", [])
    output = json.loads(serialize_output_message(with_content, max_chars=10_000))
    assert "tool_calls" not in output[0]


@pytest.mark.parametrize(
    ("value", "placeholder"),
    [
        (_ExplodingList(RuntimeError("iteration unavailable")), "<_ExplodingList>"),
        (_ExplodingSet(RuntimeError("iteration unavailable")), "<_ExplodingSet>"),
    ],
)
def test_json_collections_degrade_iteration_failures_stably(
    value: Any,
    placeholder: str,
) -> None:
    output = json.loads(
        serialize_output_message(
            {"tool_calls": [{"name": "tool", "arguments": value}]},
            max_chars=10_000,
        )
    )

    assert output[0]["tool_calls"][0]["arguments"] == placeholder


@pytest.mark.parametrize(
    "value",
    [
        _ExplodingList(KeyboardInterrupt("stop")),
        _ExplodingSet(SystemExit("stop")),
    ],
)
def test_json_collection_iteration_propagates_control_exceptions(value: Any) -> None:
    with pytest.raises((KeyboardInterrupt, SystemExit)):
        serialize_output_message(
            {"tool_calls": [{"name": "tool", "arguments": value}]},
            max_chars=10_000,
        )


def test_model_dump_self_cycle_is_detected_without_recursive_descent() -> None:
    payload = json.loads(
        serialize_output_message(
            {"tool_calls": [{"name": "tool", "arguments": _SelfDump()}]},
            max_chars=10_000,
        )
    )

    assert payload[0]["tool_calls"][0]["arguments"] == "<cycle>"


def test_serialization_control_exceptions_are_not_swallowed() -> None:
    with pytest.raises(KeyboardInterrupt):
        message_role(
            _ProbeMapping(get_errors={"role": KeyboardInterrupt("stop")})
        )
    with pytest.raises(SystemExit):
        serialize_input_messages(
            [
                {
                    "role": "user",
                    "content": _ProbeMapping(items_error=SystemExit("stop")),
                }
            ],
            max_chars=10_000,
        )
    with pytest.raises(asyncio.CancelledError):
        serialize_output_message(
            {
                "tool_calls": [
                    {
                        "name": "tool",
                        "arguments": _ControlDump(asyncio.CancelledError()),
                    }
                ]
            },
            max_chars=10_000,
        )
    with pytest.raises(KeyboardInterrupt):
        serialize_input_messages(
            _ExplodingIterable(KeyboardInterrupt("stop")), max_chars=10_000
        )


@pytest.mark.parametrize(
    "serializer",
    [
        serialize_input_messages,
        serialize_output_message,
        serialize_tool_definitions,
    ],
)
def test_message_serialization_has_deterministic_length_boundaries(serializer) -> None:
    payload = [{"role": "user", "content": "abcdef"}]
    if serializer is serialize_output_message:
        payload = {"content": "abcdef"}
    elif serializer is serialize_tool_definitions:
        payload = [{"function": {"name": "tool", "description": "abcdef"}}]

    with pytest.raises(ValueError, match="max_chars"):
        serializer(payload, max_chars=-1)
    assert serializer(payload, max_chars=0) == ""
    full = serializer(payload, max_chars=10_000)
    assert serializer(payload, max_chars=7) == full[:7]


def test_output_serialization_supports_object_tool_calls_and_reasoning() -> None:
    result = SimpleNamespace(
        content="done",
        reasoning_content="because",
        tool_calls=[
            SimpleNamespace(id="call-1", name="search", arguments={"q": "中文"})
        ],
    )

    payload = json.loads(serialize_output_message(result, max_chars=10_000))

    assert payload == [
        {
            "parts": [
                {"content": "done", "type": "text"},
                {"content": "because", "type": "reasoning"},
            ],
            "role": "assistant",
            "tool_calls": [
                {"arguments": {"q": "中文"}, "id": "call-1", "name": "search"}
            ],
        }
    ]


def test_prompt_and_completion_are_serialized_independently() -> None:
    prompt = serialize_input_messages(
        [{"role": "user", "content": "prompt-secret"}], max_chars=10_000
    )
    completion = serialize_output_message(
        {"content": "completion-secret"}, max_chars=10_000
    )

    assert "prompt-secret" in prompt
    assert "completion-secret" not in prompt
    assert "completion-secret" in completion
    assert "prompt-secret" not in completion


def test_message_count_and_length_do_not_require_full_serialization() -> None:
    messages = [
        {"role": "user", "content": "abc"},
        SimpleNamespace(role="assistant", content="你好"),
    ]

    count = len(messages)
    total_length = sum(len(message_content(message)) for message in messages)

    assert count == 2
    assert total_length == 5


def test_tool_definitions_are_canonical_for_dict_and_object_shapes() -> None:
    object_tool = SimpleNamespace(
        type="function",
        name="weather",
        description="Weather lookup",
        parameters={"type": "object"},
    )
    tools = [
        {
            "function": {
                "parameters": {"type": "object"},
                "description": "Search",
                "name": "search",
            },
            "type": "function",
        },
        object_tool,
        {"type": "function", "function": {"description": "missing name"}},
    ]

    payload = json.loads(serialize_tool_definitions(tools, max_chars=10_000))

    assert payload == [
        {
            "description": "Search",
            "name": "search",
            "parameters": {"type": "object"},
            "type": "function",
        },
        {
            "description": "Weather lookup",
            "name": "weather",
            "parameters": {"type": "object"},
            "type": "function",
        },
    ]


def test_all_serializers_handle_unserializable_values_and_4096_limit() -> None:
    result = {
        "content": _ExplodingString(),
        "tool_calls": [
            {"id": "call", "name": "tool", "arguments": {"value": "x" * 8_000}}
        ],
    }
    output = serialize_output_message(result, max_chars=4096)
    definitions = serialize_tool_definitions(
        [{"type": "function", "function": {"name": "tool", "parameters": _Opaque()}}],
        max_chars=4096,
    )

    assert len(output) == 4096
    assert len(definitions) <= 4096
    assert "<_Opaque>" in definitions


def test_tool_arguments_and_results_follow_message_char_budget() -> None:
    """Tool single-item fields (result content / call arguments) share the
    message-level char budget instead of a fixed 4096 cap: with headroom the
    payload passes through untruncated, and a tighter budget truncates each
    item to the budget."""
    tool_result = json.loads(
        serialize_input_messages(
            [{"role": "tool", "content": "r" * 5_000}], max_chars=10_000
        )
    )
    output = json.loads(
        serialize_output_message(
            {
                "tool_calls": [
                    {"name": "tool", "arguments": {"value": "a" * 5_000}}
                ]
            },
            max_chars=10_000,
        )
    )

    # Headroom: neither item is truncated; the arguments dict is small enough
    # to fit the budget and is kept structured (see _bounded_value).
    assert len(tool_result[0]["parts"][0]["content"]) == 5_000
    assert output[0]["tool_calls"][0]["arguments"] == {"value": "a" * 5_000}

    tight_result = serialize_input_messages(
        [{"role": "tool", "content": "r" * 5_000}], max_chars=4_000
    )
    tight_output = serialize_output_message(
        {
            "tool_calls": [
                {"name": "tool", "arguments": {"value": "a" * 5_000}}
            ]
        },
        max_chars=4_000,
    )

    # Tight budget: the budget still caps the serialized output (best-effort
    # truncation, same assertion style as the fixed-budget test above).
    assert len(tight_result) == 4_000
    assert len(tight_output) == 4_000


@pytest.mark.parametrize(
    ("result", "expected"),
    [
        (
            {
                "tool_calls": [
                    {"function": {"name": "search"}},
                    {"name": "weather"},
                    {"name": ""},
                ]
            },
            ("tool_call", ["search", "weather"]),
        ),
        (SimpleNamespace(content="answer", tool_calls=[]), ("answer", [])),
        (
            SimpleNamespace(
                content="",
                tool_calls=[SimpleNamespace(name="object-tool", id="call")],
            ),
            ("tool_call", ["object-tool"]),
        ),
        ({"content": "", "tool_calls": []}, ("unknown", [])),
        (None, ("unknown", [])),
    ],
)
def test_decision_classification_covers_tool_answer_and_unknown(result, expected) -> None:
    assert classify_decision(result) == expected


def test_decision_materializes_empty_tool_call_generators_before_classifying() -> None:
    assert classify_decision(
        {"content": "answer", "tool_calls": (call for call in ())}
    ) == ("answer", [])
    assert classify_decision(
        {"content": "", "tool_calls": (call for call in ())}
    ) == ("unknown", [])


def test_usage_extracts_nested_sdk_and_provider_specific_fields() -> None:
    result = SimpleNamespace(
        usage_metadata=SimpleNamespace(
            input_tokens=12,
            output_tokens=7,
            prompt_tokens_details=SimpleNamespace(cached_tokens=5),
            cache_creation_input_tokens=3,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=2),
        )
    )

    assert extract_usage(result) == UsageBreakdown(
        input_tokens=12,
        output_tokens=7,
        total_tokens=19,
        cache_read_input_tokens=5,
        cache_creation_input_tokens=3,
        reasoning_output_tokens=2,
    )


def test_usage_supports_flat_dict_aliases_and_never_raises_for_invalid_values() -> None:
    result = {
        "prompt_tokens": "8",
        "completion_tokens": 4.0,
        "cache_tokens": "3",
        "cache_creation_input_tokens": -2,
        "reasoning_tokens": _ExplodingString(),
    }

    assert extract_usage(result) == UsageBreakdown(
        input_tokens=8,
        output_tokens=4,
        total_tokens=12,
        cache_read_input_tokens=3,
        cache_creation_input_tokens=0,
        reasoning_output_tokens=0,
    )
    assert extract_usage(None) == UsageBreakdown()
    assert extract_usage({"usage_metadata": {"input_tokens": True}}) == UsageBreakdown()


def test_usage_degrades_broken_top_level_and_nested_mapping_reads() -> None:
    broken_top = _ProbeMapping(get_errors={"*": RuntimeError("unavailable")})
    partly_broken_usage = _ProbeMapping(
        {"input_tokens": 9, "output_tokens": 4},
        get_errors={"input_tokens": RuntimeError("input unavailable")},
    )

    assert extract_usage(broken_top) == UsageBreakdown()
    assert extract_usage({"usage_metadata": partly_broken_usage}) == UsageBreakdown(
        output_tokens=4,
        total_tokens=4,
    )


def test_context_tokens_classify_all_components_and_each_skill(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter", lambda: None
    )
    messages = [
        {"role": "system", "content": "s" * 8},
        {"role": "user", "content": "u" * 8},
        {"role": "assistant", "content": "a" * 8},
        {"role": "tool", "content": "t" * 8},
        {
            "role": "tool",
            "content": "x" * 8,
            "metadata": {"is_skill_body": True, "skill_name": "alpha"},
        },
        {
            "role": "tool",
            "content": "y" * 8,
            "metadata": {"original_is_skill_body": True, "skill_name": "beta"},
        },
        {
            "role": "system",
            "content": "p" * 8,
            "metadata": {"active_skill_pin": "beta"},
        },
    ]
    tools = [{"type": "function", "function": {"name": "search"}}]

    breakdown = count_context_tokens(messages, tools)

    assert breakdown.system_prompt == 2
    assert breakdown.user_messages == 2
    assert breakdown.assistant_messages == 2
    assert breakdown.tool_results == 2
    assert breakdown.skill == 6
    assert breakdown.tool_definitions >= 0
    assert breakdown.per_skill_tokens == (("alpha", 2), ("beta", 4))
    assert breakdown.message_total == 14
    assert breakdown.total == breakdown.message_total + breakdown.tool_definitions


def test_context_tokens_infer_skill_body_from_assistant_tool_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter", lambda: None
    )
    messages = [
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "call-skill",
                    "function": {
                        "name": "skill_tool",
                        "arguments": '{"skill_name":"forecast"}',
                    },
                }
            ],
        },
        {
            "role": "tool",
            "tool_call_id": "call-skill",
            "content": "x" * 8,
        },
    ]

    breakdown = count_context_tokens(messages, [])

    assert breakdown.skill == 2
    assert breakdown.tool_results == 0
    assert breakdown.per_skill_tokens == (("forecast", 2),)


def test_context_tokens_degrade_broken_metadata_mapping_reads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter", lambda: None
    )
    metadata = _ProbeMapping(get_errors={"*": RuntimeError("unavailable")})

    breakdown = count_context_tokens(
        [{"role": "tool", "content": "12345678", "metadata": metadata}], []
    )

    assert breakdown.tool_results == 2
    assert breakdown.skill == 0


def test_context_tokens_use_tiktoken_for_text_extras_and_tool_definitions(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter",
        lambda: _CountingCounter(),
    )
    assistant = {
        "role": "assistant",
        "content": "body",
        "reasoning_content": "why",
        "tool_calls": [{"name": "search", "arguments": {"q": "x"}}],
    }

    breakdown = count_context_tokens([assistant], [{"name": "tool"}])

    assert breakdown.assistant_messages > len("body")
    assert breakdown.tool_definitions > 0
    assert breakdown.total == (
        breakdown.system_prompt
        + breakdown.user_messages
        + breakdown.assistant_messages
        + breakdown.tool_results
        + breakdown.skill
        + breakdown.tool_definitions
    )


def test_dict_tool_tokens_use_the_actual_nested_llm_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _RecordingCounter()
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter",
        lambda: counter,
    )
    search_tool = {
        "type": "function",
        "function": {
            "name": "search",
            "description": "Lookup 中文",
            "parameters": {"type": "object", "required": ["query"]},
        },
    }
    weather_tool = {
        "type": "function",
        "function": {
            "name": "weather",
            "description": "Weather lookup",
            "parameters": {"type": "object", "required": ["city"]},
        },
    }
    search_payload = json.dumps(
        search_tool, ensure_ascii=False, separators=(",", ":")
    )
    weather_payload = json.dumps(
        weather_tool, ensure_ascii=False, separators=(",", ":")
    )
    search_piece = f"<|start|>functions.search:0\n{search_payload}<|end|>"
    weather_piece = f"<|start|>functions.weather:1\n{weather_payload}<|end|>"

    breakdown = count_context_tokens([], [search_tool, weather_tool])

    assert counter.inputs == [search_piece, weather_piece]
    assert dict(breakdown.per_tool_tokens) == {
        "search": len(search_piece),
        "weather": len(weather_piece),
    }
    assert breakdown.tool_definitions == len(search_piece) + len(weather_piece) + 3


def test_object_tool_tokens_rebuild_the_actual_nested_llm_payload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _RecordingCounter()
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter",
        lambda: counter,
    )
    tool = SimpleNamespace(
        type="function",
        name="weather",
        description="Weather 中文",
        parameters={"type": "object", "properties": {"city": {"type": "string"}}},
    )
    payload = json.dumps(
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "Weather 中文",
                "parameters": {
                    "type": "object",
                    "properties": {"city": {"type": "string"}},
                },
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    expected_piece = f"<|start|>functions.weather:0\n{payload}<|end|>"

    breakdown = count_context_tokens([], [tool])

    assert counter.inputs == [expected_piece]
    assert breakdown.tool_definitions == len(expected_piece) + 3


def test_tool_definition_fallback_matches_enterprise_shapes_without_double_divide(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter", lambda: None
    )
    dict_tool = {
        "type": "function",
        "function": {"name": "search", "description": "Lookup"},
    }
    object_tool = SimpleNamespace(
        type="function",
        name="weather",
        description="Weather",
        parameters={"type": "object"},
    )
    object_payload = json.dumps(
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "Weather",
                "parameters": {"type": "object"},
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    object_piece = f"<|start|>functions.weather:0\n{object_payload}<|end|>"

    dict_breakdown = count_context_tokens([], [dict_tool])
    object_breakdown = count_context_tokens([], [object_tool])

    assert dict_breakdown.tool_definitions == (
        len(json.dumps(dict_tool, ensure_ascii=False)) // 4
    )
    assert object_breakdown.tool_definitions == len(object_piece) // 4


def test_tool_definition_counter_failure_falls_back_without_tiktoken_priming(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter",
        lambda: _FailingCounter(),
    )
    tool = {
        "type": "function",
        "function": {"name": "search", "description": "Lookup"},
    }

    breakdown = count_context_tokens([], [tool])

    assert breakdown.tool_definitions == (
        len(json.dumps(tool, ensure_ascii=False)) // 4
    )
    assert breakdown.per_tool_tokens == (
        ("search", len(json.dumps(tool, ensure_ascii=False)) // 4),
    )


def test_unnamed_tool_tokens_use_stable_indexed_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter", lambda: None
    )

    breakdown = count_context_tokens([], [{"type": "function"}, {}])

    assert [name for name, _ in breakdown.per_tool_tokens] == [
        "_unknown_0",
        "_unknown_1",
    ]


def test_partial_tool_counter_failure_recomputes_the_whole_batch_as_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    counter = _PartiallyFailingCounter()
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter",
        lambda: counter,
    )
    dict_tool = {
        "type": "function",
        "function": {"name": "search", "description": "Lookup"},
    }
    object_tool = SimpleNamespace(
        type="function",
        name="weather",
        description="Weather",
        parameters={"type": "object"},
    )
    object_payload = json.dumps(
        {
            "type": "function",
            "function": {
                "name": "weather",
                "description": "Weather",
                "parameters": {"type": "object"},
            },
        },
        ensure_ascii=False,
        separators=(",", ":"),
    )
    object_piece = f"<|start|>functions.weather:1\n{object_payload}<|end|>"
    expected_fallback = (
        len(json.dumps(dict_tool, ensure_ascii=False)) // 4
        + len(object_piece) // 4
    )

    breakdown = count_context_tokens([], [dict_tool, object_tool])

    assert len(counter.inputs) == 2
    assert breakdown.tool_definitions == expected_fallback


def test_context_token_count_failure_uses_nonnegative_len_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter",
        lambda: _FailingCounter(),
    )

    breakdown = count_context_tokens(
        [{"role": "user", "content": "12345678"}], []
    )

    assert breakdown.user_messages == 2
    assert breakdown.total == 2


def test_token_counter_is_initialized_once_across_concurrent_threads(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openjiuwen.core.context_engine as context_engine
    import jiuwenswarm.telemetry.enrichment.tokens as token_helpers

    workers = 8
    start = threading.Barrier(workers + 1)
    factory_barrier = threading.Barrier(workers)
    calls_lock = threading.Lock()
    calls = 0
    singleton = object()

    def factory(*, model: str):
        nonlocal calls
        assert model == "gpt-4"
        with calls_lock:
            calls += 1
        try:
            factory_barrier.wait(timeout=1)
        except threading.BrokenBarrierError:
            pass
        return singleton

    def load_counter():
        start.wait()
        return token_helpers._get_token_counter()

    monkeypatch.setattr(
        token_helpers, "_token_counter", token_helpers._TOKEN_COUNTER_UNSET
    )
    monkeypatch.setattr(context_engine, "TiktokenCounter", factory)
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = [executor.submit(load_counter) for _ in range(workers)]
        start.wait()
        results = [future.result(timeout=3) for future in futures]

    assert calls == 1
    assert all(result is singleton for result in results)


def test_late_counter_initialization_failure_cannot_overwrite_committed_success(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import openjiuwen.core.context_engine as context_engine
    import jiuwenswarm.telemetry.enrichment.tokens as token_helpers

    start = threading.Barrier(3)
    second_entered = threading.Event()
    success_returned = threading.Event()
    calls_lock = threading.Lock()
    calls = 0
    singleton = object()

    def factory(*, model: str):
        nonlocal calls
        assert model == "gpt-4"
        with calls_lock:
            calls += 1
            call_index = calls
        if call_index == 1:
            second_entered.wait(timeout=1)
            return singleton
        second_entered.set()
        success_returned.wait(timeout=1)
        raise RuntimeError("late initialization failure")

    def load_counter():
        start.wait()
        result = token_helpers._get_token_counter()
        if result is singleton:
            success_returned.set()
        return result

    monkeypatch.setattr(
        token_helpers, "_token_counter", token_helpers._TOKEN_COUNTER_UNSET
    )
    monkeypatch.setattr(context_engine, "TiktokenCounter", factory)
    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(load_counter) for _ in range(2)]
        start.wait()
        results = [future.result(timeout=3) for future in futures]

    assert calls == 1
    assert all(result is singleton for result in results)
    assert token_helpers._get_token_counter() is singleton


def test_skill_load_extracts_enterprise_tool_message_metadata() -> None:
    inputs = SimpleNamespace(
        tool_msg=SimpleNamespace(
            metadata={
                "original_is_skill_body": True,
                "skill_name": "writer",
                "skill_id": "skill-id",
                "skill_version": "2.1",
                "relative_file_path": "writer/SKILL.md",
            }
        )
    )

    assert extract_skill("skill_tool", inputs, None) == SkillObservation(
        name="writer",
        skill_id="skill-id",
        version="2.1",
        path="writer/SKILL.md",
        loaded=True,
        released=False,
    )


def test_skill_load_supports_arguments_and_real_tool_output_data() -> None:
    inputs = {
        "tool_call": {
            "arguments": '{"skill_name":"analyst","relative_file_path":"SKILL.md"}'
        }
    }
    result = {
        "success": True,
        "data": {"skill_directory": "/skills/analyst", "skill_content": "body"},
    }

    observation = extract_skill("skill_tool", inputs, result)

    assert observation is not None
    assert observation.name == "analyst"
    assert observation.skill_id.startswith("skill_")
    assert observation.path == "SKILL.md"
    assert observation.loaded is True
    assert observation.released is False


@pytest.mark.parametrize(
    ("tool_name", "released"),
    [("skill_tool", False), ("skill_complete", True)],
)
def test_skill_supports_real_tool_wrapper_args_and_tool_output(
    tool_name: str,
    released: bool,
) -> None:
    inputs = (
        (
            {
                "skill_name": "forecast",
                "skill_id": "skill-forecast",
                "version": "v2",
                "relative_file_path": "forecast/SKILL.md",
            },
        ),
        {},
    )
    result = ToolOutput(
        success=True,
        data={"skill_directory": "/skills/forecast", "skill_content": "body"},
    )

    assert extract_skill(tool_name, inputs, result) == SkillObservation(
        name="forecast",
        skill_id="skill-forecast",
        version="v2",
        path="forecast/SKILL.md",
        loaded=not released,
        released=released,
    )


def test_malformed_real_tool_wrapper_inputs_do_not_raise() -> None:
    assert (
        extract_skill(
            "skill_tool",
            ((object(),), {"inputs": object()}),
            ToolOutput(success=True, data={}),
        )
        is None
    )


@pytest.mark.parametrize(
    ("result", "expected_loaded"),
    [
        ({"success": True, "data": {}}, True),
        (SimpleNamespace(success=True, error=None, data={}), True),
        ({"success": False, "error": "load failed"}, False),
        (SimpleNamespace(success=False, error="load failed", data={}), False),
        ({"success": True, "error": "contradictory failure"}, False),
    ],
)
def test_skill_loaded_reflects_real_dict_and_object_tool_outcomes(
    result: Any,
    expected_loaded: bool,
) -> None:
    inputs = {
        "arguments": {
            "skill_name": "writer",
            "skill_id": "writer-id",
            "skill_version": "2",
            "relative_file_path": "writer/SKILL.md",
        }
    }

    observation = extract_skill("skill_tool", inputs, result)

    assert observation == SkillObservation(
        name="writer",
        skill_id="writer-id",
        version="2",
        path="writer/SKILL.md",
        loaded=expected_loaded,
        released=False,
    )


def test_skill_arguments_without_result_do_not_claim_a_successful_load() -> None:
    observation = extract_skill(
        "skill_tool",
        {"arguments": {"skill_name": "writer", "skill_version": "2"}},
        None,
    )

    assert observation is not None
    assert observation.name == "writer"
    assert observation.version == "2"
    assert observation.loaded is False


def test_explicit_skill_failure_overrides_skill_body_metadata() -> None:
    inputs = SimpleNamespace(
        tool_msg=SimpleNamespace(
            metadata={
                "is_skill_body": True,
                "skill_name": "writer",
                "skill_version": "2",
            }
        )
    )

    observation = extract_skill(
        "skill_tool", inputs, SimpleNamespace(success=False, error="failed")
    )

    assert observation is not None
    assert observation.name == "writer"
    assert observation.loaded is False


def test_skill_extraction_degrades_broken_result_mapping_reads() -> None:
    result = _ProbeMapping(get_errors={"*": RuntimeError("unavailable")})

    observation = extract_skill(
        "skill_tool", {"arguments": {"skill_name": "writer"}}, result
    )

    assert observation is not None
    assert observation.name == "writer"
    assert observation.loaded is False


def test_skill_release_extracts_dict_arguments_and_nested_result_identity() -> None:
    inputs = {"arguments": {"skill_name": "writer"}}
    result = {"skill": {"name": "writer", "id": "release-id", "version": "3"}}

    assert extract_skill("skill_complete", inputs, result) == SkillObservation(
        name="writer",
        skill_id="release-id",
        version="3",
        path="",
        loaded=False,
        released=True,
    )
    assert extract_skill("other", inputs, result) is None
    assert extract_skill("skill_tool", {}, {}) is None


def test_skill_release_supports_nested_object_result() -> None:
    result = SimpleNamespace(
        skill=SimpleNamespace(name="object-skill", id="object-id", version="4")
    )

    assert extract_skill("skill_complete", SimpleNamespace(), result) == (
        SkillObservation(
            name="object-skill",
            skill_id="object-id",
            version="4",
            released=True,
        )
    )


def test_value_objects_are_frozen_and_helpers_do_not_mutate_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.telemetry.enrichment.tokens._get_token_counter", lambda: None
    )
    messages = [{"role": "user", "content": ["text", {"type": "image"}]}]
    tools = [{"function": {"name": "search", "parameters": {"type": "object"}}}]
    result = {"usage_metadata": {"input_tokens": 1}}
    original = copy.deepcopy((messages, tools, result))

    serialize_input_messages(messages, max_chars=10_000)
    serialize_tool_definitions(tools, max_chars=10_000)
    count_context_tokens(messages, tools)
    extract_usage(result)

    assert (messages, tools, result) == original
    breakdown = ContextTokenBreakdown()
    assert breakdown.per_tool_tokens == ()
    with pytest.raises(FrozenInstanceError):
        breakdown.system_prompt = 1  # type: ignore[misc]
