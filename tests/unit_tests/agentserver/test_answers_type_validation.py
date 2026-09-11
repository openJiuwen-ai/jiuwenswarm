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
    monkeypatch.setattr(interface_module, "build_user_prompt", lambda *args, **kwargs: "stub-prompt")
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
