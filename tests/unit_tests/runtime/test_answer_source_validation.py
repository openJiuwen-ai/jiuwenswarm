# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""chat.send 携带 answers 时 source 白名单校验（issue #6285、#6857）。

非法 source（如 "not_a_source"）的历史行为：既不被识别为中断恢复、也不被
拒绝，分类为普通回合后排在仍在等待该答案的交互之后，会话死锁且客户端收不
到任何结束帧。校验必须在 lane 准入之前完成，保证客户端必然收到错误帧。

#6857 补充：answers 为 mapping 时同样会绕过恢复识别并卡住（运行时只把非空
list 识别为中断恢复），因此 mapping 载荷无论 source 是什么一律拒绝；
request_id 也不再作为校验前提。
"""

from __future__ import annotations

import pytest

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

    def test_missing_request_id_with_known_source_passes(self) -> None:
        validate_answer_source(
            _answer_params(request_id="", source="client_tool_interrupt")
        )

    def test_unknown_source_without_answers_passes(self) -> None:
        """Ordinary turns may tag themselves (e.g. video_tool); only answers are checked."""
        validate_answer_source({"query": "hi", "source": "video_tool"})

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
