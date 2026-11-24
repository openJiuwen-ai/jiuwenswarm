# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""SkillTurbo chat.error 出口复用 server 侧统一构造器的词法兜底。"""

from __future__ import annotations

from types import SimpleNamespace

from jiuwenswarm.server.runtime.skill_turbo.executor import SkillTurboExecutor

_IMAGE_UNSAFE_MESSAGE = (
    "BadRequestError: Error code: 400 - the content of image_url.url field is unsafe"
)


class _ExecutorStub:
    """只实现被调方法依赖的成员，避免构造完整 Executor。"""

    def _make_chunk(self, request_id, channel_id, payload, is_complete=False):
        return payload

    @staticmethod
    def _display_name(name: str) -> str:
        return name


def test_make_error_chunk_adds_image_unsafe_code() -> None:
    payload = SkillTurboExecutor._make_error_chunk(
        _ExecutorStub(), "req", "chan", _IMAGE_UNSAFE_MESSAGE
    )

    assert payload["event_type"] == "chat.error"
    assert payload["error"] == _IMAGE_UNSAFE_MESSAGE
    assert payload["code"] == "image_url_unsafe"


def test_make_error_chunk_keeps_non_image_error_shape() -> None:
    payload = SkillTurboExecutor._make_error_chunk(
        _ExecutorStub(), "req", "chan", "model timeout"
    )

    assert payload == {"event_type": "chat.error", "error": "model timeout"}


def test_make_node_error_chunk_adds_code_and_node_metadata() -> None:
    payload = SkillTurboExecutor._make_node_error_chunk(
        _ExecutorStub(),
        "req",
        "chan",
        SimpleNamespace(plan_name="p1"),
        _IMAGE_UNSAFE_MESSAGE,
        "task-1",
    )

    assert payload["event_type"] == "chat.error"
    assert payload["code"] == "image_url_unsafe"
    assert payload["plan_name"] == "p1"
    assert payload["task_id"] == "task-1"
