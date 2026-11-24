# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.send 携带 answers 时 source 白名单校验（issue #6285、#6857）。

非法 source（如 "not_a_source"）的历史行为：既不被识别为中断恢复、也不被
拒绝，分类为普通回合后排在仍在等待该答案的交互之后，会话死锁且客户端收不
到任何结束帧。校验必须在 lane 准入之前完成，保证客户端必然收到错误帧。

#6857 补充：answers 为非空 mapping 时同样会绕过恢复识别并卡住（运行时只把非空
list 识别为中断恢复），因此非空 mapping、字符串及其他非 list 载荷无论 source
是什么一律拒绝。校验不再自建白名单判定，而是直接复用 lane 准入使用的
`agent_gateway.common.interrupt_resume.answers_paused_calls`：除
client_tool_interrupt 外，缺少 request_id 的载荷不会被识别为恢复，同样会卡住。
空 mapping 与空 list 一样视为「无 answers」；空白 source 不声明恢复，保持普通
回合行为。
"""

from __future__ import annotations

import pytest

from agent_gateway.common.interrupt_resume import (
    INTERRUPT_RESUME_SOURCES,
    answers_paused_calls,
)
from jiuwenswarm.common.schema.agent import AgentRequest
from jiuwenswarm.common.schema.message import ReqMethod
from jiuwenswarm.runtime.interaction import (
    ANSWER_SOURCE_WHITELIST,
    InteractionAnswerError,
    validate_answer_source,
)
from jiuwenswarm.runtime.service import AgentRuntime


def _answer_params(**overrides):
    params = {
        "request_id": "interrupt-1",
        "answers": [{"selected_options": ["session_allow"], "custom_input": ""}],
        "source": "permission_interrupt",
    }
    params.update(overrides)
    return params


class TestValidateAnswerSource:
    def test_unknown_source_is_rejected(self) -> None:
        params = _answer_params(source="not_a_source")

        with pytest.raises(InteractionAnswerError) as excinfo:
            validate_answer_source(params)

        message = str(excinfo.value)
        assert "not_a_source" in message
        for valid in ANSWER_SOURCE_WHITELIST:
            assert valid in message

    def test_all_whitelist_sources_pass(self) -> None:
        for source in ANSWER_SOURCE_WHITELIST:
            validate_answer_source(_answer_params(source=source))

    def test_missing_request_id_still_rejects_unknown_source(self) -> None:
        with pytest.raises(InteractionAnswerError):
            validate_answer_source(_answer_params(request_id="", source="not_a_source"))

    def test_mapping_answers_without_request_id_are_rejected(self) -> None:
        """#6857: a dict answers payload with no request_id still hangs without this."""
        with pytest.raises(InteractionAnswerError):
            validate_answer_source(
                {"answers": {"q": "v"}, "source": "not_a_source"}
            )

    def test_mapping_answers_with_whitelisted_source_are_rejected(self) -> None:
        """A whitelisted source must not launder a shape no resume can accept."""
        for source in ANSWER_SOURCE_WHITELIST:
            with pytest.raises(InteractionAnswerError):
                validate_answer_source(
                    {"request_id": "interrupt-1", "answers": {"q": "v"}, "source": source}
                )

    def test_mapping_answers_without_source_are_rejected(self) -> None:
        """Mapping answers are rejected on shape alone, before the source check."""
        with pytest.raises(InteractionAnswerError):
            validate_answer_source({"answers": {"q": "v"}})

    def test_missing_request_id_with_client_tool_source_passes(self) -> None:
        validate_answer_source(
            _answer_params(request_id="", source="client_tool_interrupt")
        )

    @pytest.mark.parametrize(
        "source", sorted(ANSWER_SOURCE_WHITELIST - {"client_tool_interrupt"})
    )
    def test_missing_request_id_with_whitelisted_source_is_rejected(
        self, source
    ) -> None:
        """Only client_tool_interrupt may resume without naming a question."""
        with pytest.raises(InteractionAnswerError) as excinfo:
            validate_answer_source(_answer_params(request_id="", source=source))

        assert "request_id" in str(excinfo.value)

    @pytest.mark.parametrize(
        "answers", ["answer", b"answer", 1, ({"selected_options": []},)]
    )
    def test_non_list_answers_are_rejected(self, answers) -> None:
        """A shape no resume predicate accepts must not reach lane admission."""
        with pytest.raises(InteractionAnswerError):
            validate_answer_source(_answer_params(answers=answers))

    @pytest.mark.parametrize("source", sorted(INTERRUPT_RESUME_SOURCES))
    @pytest.mark.parametrize("request_id", ["interrupt-1", "   ", ""])
    def test_guard_matches_the_lane_resume_predicate(
        self, source, request_id
    ) -> None:
        """Accept exactly what lane admission resumes, from the same primitive."""
        params = _answer_params(request_id=request_id, source=source)

        if answers_paused_calls(params):
            validate_answer_source(params)
            return
        with pytest.raises(InteractionAnswerError):
            validate_answer_source(params)

    def test_unknown_source_without_answers_passes(self) -> None:
        """Ordinary turns may tag themselves (e.g. video_tool); only answers are checked."""
        validate_answer_source({"query": "hi", "source": "video_tool"})

    def test_empty_mapping_answers_are_ignored(self) -> None:
        """An empty mapping carries no answers; only content is rejected."""
        validate_answer_source(_answer_params(answers={}, source="not_a_source"))

    def test_empty_answers_are_ignored(self) -> None:
        validate_answer_source(_answer_params(answers=[], source="not_a_source"))

    def test_blank_source_keeps_legacy_behavior(self) -> None:
        validate_answer_source(_answer_params(source=""))

    def test_non_mapping_params_are_ignored(self) -> None:
        validate_answer_source(None)
        validate_answer_source("permission_interrupt")


class TestAgentRuntimeValidateAnswerSource:
    @staticmethod
    def _request(req_method: ReqMethod, params: dict) -> AgentRequest:
        return AgentRequest(
            request_id="sdk-request",
            channel_id="web",
            session_id="session-1",
            req_method=req_method,
            params=params,
            is_stream=True,
        )

    def test_chat_send_with_unknown_source_is_rejected(self) -> None:
        request = self._request(
            ReqMethod.CHAT_SEND, _answer_params(source="not_a_source")
        )

        with pytest.raises(InteractionAnswerError):
            AgentRuntime._validate_answer_source(request)

    def test_chat_send_with_mapping_answers_and_no_mode_is_rejected(self) -> None:
        """#6857: the reported team-mode frame carries dict answers and no mode."""
        request = self._request(
            ReqMethod.CHAT_SEND,
            {
                "session_id": "session-1",
                "query": "",
                "content": "回答",
                "source": "not_a_source",
                "answers": {"q": "v"},
                "enable_swarmflow": False,
            },
        )

        with pytest.raises(InteractionAnswerError):
            AgentRuntime._validate_answer_source(request)

    def test_chat_send_with_valid_source_passes(self) -> None:
        request = self._request(ReqMethod.CHAT_SEND, _answer_params())

        AgentRuntime._validate_answer_source(request)

    def test_non_chat_send_methods_are_skipped(self) -> None:
        request = self._request(
            ReqMethod.HISTORY_GET, _answer_params(source="not_a_source")
        )

        AgentRuntime._validate_answer_source(request)
