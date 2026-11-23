from __future__ import annotations

import pytest

from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.server.runtime.agent_adapter import interface as interface_module


def _make_swarm(monkeypatch: pytest.MonkeyPatch) -> interface_module.JiuWenSwarm:
    monkeypatch.setattr(
        interface_module,
        "get_config",
        lambda: {"preferred_language": "zh", "memory": {"mode": "disabled"}},
    )
    monkeypatch.setattr(interface_module, "get_memory_mode", lambda _config: "disabled")
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda *args, **kwargs: interface_module.BuiltUserPrompt(
        user_query="stub-prompt",
        context={},
        interaction_prefix="",
        statusline_directive="",
    ))
    return interface_module.JiuWenSwarm()


def _make_request(params: dict) -> AgentRequest:
    return AgentRequest(
        request_id="req-answers-type",
        channel_id="web",
        session_id="sess-answers-type",
        params=params,
        is_stream=True,
    )


@pytest.mark.parametrize(
    "bad_answers", ["not-a-list", 123, True, 1.5, {"问题1": "选项A"}]
)
def test_build_inputs_rejects_invalid_answers_type(
    monkeypatch: pytest.MonkeyPatch, bad_answers
) -> None:
    swarm = _make_swarm(monkeypatch)
    request = _make_request(
        {
            "query": "hello",
            "answers": bad_answers,
            "request_id": "call-1",
            "source": "ask_user_interrupt",
        }
    )
    with pytest.raises(interface_module._InvalidAnswersTypeError) as exc_info:
        swarm._build_inputs(request)
    assert type(bad_answers).__name__ in str(exc_info.value)


@pytest.mark.parametrize(
    "answers",
    [
        [{"selected_options": ["允许"], "custom_input": ""}],
        [],
        None,
    ],
)
def test_build_inputs_accepts_valid_answers(
    monkeypatch: pytest.MonkeyPatch, answers
) -> None:
    swarm = _make_swarm(monkeypatch)
    params: dict = {"query": "hello", "request_id": "call-1", "source": "ask_user_interrupt"}
    if answers is not None:
        params["answers"] = answers
    inputs, _memory_mode, _raw_query = swarm._build_inputs(_make_request(params))
    assert "query" in inputs


def test_empty_expert_confirmation_answers_build_a_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    from jiuwenswarm.symphony.expert_team import EXPERT_PLAN_ACTION_KEY

    swarm = _make_swarm(monkeypatch)
    request_id = "symphony-plan:displayed-plan"
    inputs, _, _ = swarm._build_inputs(_make_request({
        "query": "", "request_id": request_id, "source": "confirm_interrupt", "answers": [],
    }))
    assert inputs["query"].user_inputs[request_id] == {
        "approved": False, "auto_confirm": False, "feedback": "", EXPERT_PLAN_ACTION_KEY: "decline",
    }


@pytest.mark.parametrize("source, request_id", [
    ("confirm_interrupt", "ordinary-call"),
    ("confirm_interrupt", "symphony-unrelated:call"),
    ("permission_interrupt", "ordinary-call"),
    ("permission_interrupt", "symphony-plan:displayed-plan"),
    ("ask_user_interrupt", "ordinary-call"),
])
def test_empty_nonexpert_answers_keep_normal_prompt_path(monkeypatch, source, request_id):
    swarm = _make_swarm(monkeypatch)
    inputs, _, _ = swarm._build_inputs(_make_request({
        "query": "hello", "request_id": request_id, "source": source, "answers": [],
    }))
    assert inputs["query"] == "stub-prompt"


@pytest.mark.parametrize("source", ["confirm_interrupt", "permission_interrupt"])
@pytest.mark.parametrize("value, custom_input, approved, feedback", [
    ("本次允许", "", True, ""),
    ("确认并开始", "", False, "未知选项: 确认并开始"),
    ("取消", "", False, "未知选项: 取消"),
    ("跳过", "", False, "未知选项: 跳过"),
    ("", "", False, "未知选项: "),
    ("取消", "保留这条反馈", False, "保留这条反馈"),
    ("拒绝", "", False, "用户拒绝"),
])
def test_nonexpert_confirmation_and_permission_keep_upstream_answer_semantics(
    source, value, custom_input, approved, feedback,
):
    answer = interface_module.JiuWenSwarm._build_interactive_input_from_answers(
        "ordinary-call", [{"selected_options": [value] if value else [], "custom_input": custom_input}],
        source=source,
    )
    assert answer.user_inputs["ordinary-call"] == {
        "approved": approved, "auto_confirm": False, "feedback": feedback,
    }


@pytest.mark.asyncio
async def test_process_message_stream_returns_failed_chunk_for_invalid_answers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class FakeAdapter:  # pragma: no cover - must not be reached
        @staticmethod
        async def process_message_stream_impl(*_args, **_kwargs):
            raise AssertionError("adapter must not be reached for invalid answers")

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    swarm = _make_swarm(monkeypatch)
    request = _make_request(
        {
            "query": "hello",
            "answers": "not-a-list",
            "request_id": "call-1",
            "source": "ask_user_interrupt",
        }
    )

    chunks = [chunk async for chunk in swarm.process_message_stream(request)]

    assert len(chunks) == 1
    assert chunks[0].is_complete is True
    payload = chunks[0].payload
    assert payload["event_type"] == "chat.error"
    assert payload["code"] == "INVALID_ARGUMENT"
    assert "must be a list" in payload["error"]


def test_empty_no_plan_confirmation_answers_remain_an_invalid_expert_choice(monkeypatch):
    from jiuwenswarm.symphony.expert_team import EXPERT_PLAN_ACTION_KEY

    swarm = _make_swarm(monkeypatch)
    request_id = "symphony-no-plan:displayed-choice"
    inputs, _, _ = swarm._build_inputs(_make_request({
        "query": "", "request_id": request_id, "source": "confirm_interrupt", "answers": [],
    }))
    assert inputs["query"].user_inputs[request_id] == {
        "approved": False, "auto_confirm": False, "feedback": "", EXPERT_PLAN_ACTION_KEY: "invalid",
    }


def test_build_inputs_rejects_unknown_interrupt_source_without_query(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm = _make_swarm(monkeypatch)
    request = _make_request({
        "query": "",
        "source": "unknown_interrupt",
        "request_id": "pending-card",
        "answers": [{"selected_options": ["本次允许"]}],
    })

    with pytest.raises(interface_module._InvalidChatInputError) as exc_info:
        swarm._build_inputs(request)

    assert "unsupported interrupt source" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
async def test_unknown_interrupt_source_without_query_is_invalid_argument_before_dispatch(
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
) -> None:
    class FakeAdapter:
        async def handle_heartbeat(self, _request):
            return None

        async def process_message_impl(self, *_args, **_kwargs):
            raise AssertionError("adapter must not be reached for invalid interrupt source")

        async def process_message_stream_impl(self, *_args, **_kwargs):
            raise AssertionError("adapter must not be reached for invalid interrupt source")

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    swarm = _make_swarm(monkeypatch)
    request = _make_request({
        "query": "",
        "source": "unknown_interrupt",
        "request_id": "pending-card",
        "answers": [{"selected_options": ["本次允许"]}],
    })

    if streaming:
        chunks = [chunk async for chunk in swarm.process_message_stream(request)]
        assert len(chunks) == 1
        assert chunks[0].is_complete is True
        payload = chunks[0].payload
        assert payload["event_type"] == "chat.error"
    else:
        response = await swarm.process_message(request)
        assert not response.ok
        payload = response.payload

    assert payload["code"] == "INVALID_ARGUMENT"
    assert "unsupported interrupt source" in payload["error"]


@pytest.mark.parametrize("answers", [[], None])
def test_build_inputs_rejects_unknown_interrupt_source_without_query_without_answers(
    monkeypatch: pytest.MonkeyPatch,
    answers: list | None,
) -> None:
    swarm = _make_swarm(monkeypatch)
    params = {
        "query": "",
        "source": "unknown_interrupt",
        "request_id": "pending-card",
    }
    if answers is not None:
        params["answers"] = answers
    request = _make_request(params)

    with pytest.raises(interface_module._InvalidChatInputError) as exc_info:
        swarm._build_inputs(request)

    assert "unsupported interrupt source" in str(exc_info.value)


@pytest.mark.asyncio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("answers", [[], None])
async def test_unknown_interrupt_source_without_query_is_invalid_argument_without_answers(
    monkeypatch: pytest.MonkeyPatch,
    streaming: bool,
    answers: list | None,
) -> None:
    class FakeAdapter:
        async def handle_heartbeat(self, _request):
            return None

        async def process_message_impl(self, *_args, **_kwargs):
            raise AssertionError("adapter must not be reached for invalid interrupt source")

        async def process_message_stream_impl(self, *_args, **_kwargs):
            raise AssertionError("adapter must not be reached for invalid interrupt source")

    monkeypatch.setattr(
        interface_module.JiuWenSwarm,
        "_ensure_adapter",
        lambda *_args, **_kwargs: FakeAdapter(),
    )
    monkeypatch.setattr(interface_module, "append_history_record", lambda **_kwargs: None)
    swarm = _make_swarm(monkeypatch)
    params = {
        "query": "",
        "source": "unknown_interrupt",
        "request_id": "pending-card",
    }
    if answers is not None:
        params["answers"] = answers
    request = _make_request(params)

    if streaming:
        chunks = [chunk async for chunk in swarm.process_message_stream(request)]
        assert len(chunks) == 1
        assert chunks[0].is_complete is True
        payload = chunks[0].payload
        assert payload["event_type"] == "chat.error"
    else:
        response = await swarm.process_message(request)
        assert not response.ok
        payload = response.payload

    assert payload["code"] == "INVALID_ARGUMENT"
    assert "unsupported interrupt source" in payload["error"]


@pytest.mark.parametrize("source", [" unknown_interrupt ", "unknown_interrupt\t"])
def test_build_inputs_rejects_unknown_interrupt_source_with_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
    source: str,
) -> None:
    swarm = _make_swarm(monkeypatch)
    request = _make_request({
        "query": "",
        "source": source,
        "request_id": "pending-card",
        "answers": [{"selected_options": ["本次允许"]}],
    })

    with pytest.raises(interface_module._InvalidChatInputError) as exc_info:
        swarm._build_inputs(request)

    assert "unsupported interrupt source" in str(exc_info.value)


def test_build_inputs_allows_team_plan_approval_source_with_surrounding_whitespace(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    swarm = _make_swarm(monkeypatch)
    inputs, _, _ = swarm._build_inputs(_make_request({
        "query": "",
        "source": " team_plan_approval ",
        "request_id": "team-card",
        "answers": [{"selected_options": ["approve"]}],
    }))

    assert inputs["query"] == "stub-prompt"


@pytest.mark.parametrize("text_field", ["query", "content"])
def test_unknown_interrupt_source_with_user_text_keeps_new_turn_compatibility(
    monkeypatch: pytest.MonkeyPatch,
    text_field: str,
) -> None:
    swarm = _make_swarm(monkeypatch)
    inputs, _, _ = swarm._build_inputs(_make_request({
        text_field: "new question",
        "source": "unknown_interrupt",
        "request_id": "old-card",
        "answers": [{"selected_options": ["本次允许"]}],
    }))
    assert inputs["query"] == "stub-prompt"

