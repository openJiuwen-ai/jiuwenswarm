# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import logging

from jiuwenswarm.server.runtime.agent_adapter.refusal_text_scrub import (
    RefusalTextScrub,
    could_still_be_canned_refusal,
)

_CANNED = "作为一个人工智能语言模型，我还没学习如何回答这个问题，您可以向我问一些其它的问题，我会尽力帮您解决的。"


def test_full_refusal_stays_buffered_until_drop() -> None:
    scrub = RefusalTextScrub()
    assert scrub.decide("refusal_scrub_hold") == "skip"
    assert scrub.decide("llm_output", content=_CANNED) == "buffer"
    scrub.push({"event_type": "chat.delta", "content": _CANNED})
    assert scrub.decide("refusal_scrub_drop") == "skip"
    assert scrub.take() == []


def test_answer_frame_flushes_held_prefix_when_release_is_missing() -> None:
    scrub = RefusalTextScrub()
    scrub.decide("refusal_scrub_hold")
    assert scrub.decide("llm_output", content="作为一个") == "buffer"
    scrub.push({"event_type": "chat.delta", "content": "作为一个"})
    assert scrub.decide("answer") == "flush_pass"
    assert scrub.armed is False
    assert scrub.take() == [{"event_type": "chat.delta", "content": "作为一个"}]


def test_scrub_gate_retries_failed_drop_and_does_not_release() -> None:
    import asyncio

    from openjiuwen.core.session.stream import OutputSchema

    class _Session:
        def __init__(self) -> None:
            self.events: list[str] = []
            self.fail = 0

        async def write_stream(self, schema: OutputSchema) -> None:
            if self.fail:
                self.fail -= 1
                raise RuntimeError("stream closed")
            self.events.append(schema.type)

    from jiuwenswarm.server.runtime.agent_adapter.refusal_text_scrub import RefusalScrubGate

    gate = RefusalScrubGate()
    session = _Session()

    async def _run() -> None:
        await gate.arm(session)
        session.fail = 1
        await gate.finish(session, dropped=True)
        assert session.events == ["refusal_scrub_hold"]
        await gate.finish(session, dropped=False)
        assert session.events == ["refusal_scrub_hold", "refusal_scrub_drop"]
        assert gate._hold_pending is False

    asyncio.run(_run())


def test_second_hold_warns_before_dropping_unreleased_text() -> None:
    scrub = RefusalTextScrub()
    scrub.decide("refusal_scrub_hold")
    scrub.push({"event_type": "chat.delta", "content": "作为一个"})
    records: list[logging.LogRecord] = []

    class _Handler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Handler(logging.WARNING)
    logger = logging.getLogger("jiuwenswarm.common.utils")
    logger.addHandler(handler)
    try:
        assert scrub.decide("refusal_scrub_hold") == "skip"
    finally:
        logger.removeHandler(handler)
    assert scrub.take() == []
    assert any("replaced 1 unreleased" in record.getMessage() for record in records)


def test_ordinary_text_is_released_immediately() -> None:
    scrub = RefusalTextScrub()
    scrub.decide("refusal_scrub_hold")
    assert scrub.decide("llm_output", content="我先加载技能。") == "flush_pass"
    assert scrub.armed is False
    assert could_still_be_canned_refusal("作为一个") is True
    assert could_still_be_canned_refusal("口播稿已写完。") is False
