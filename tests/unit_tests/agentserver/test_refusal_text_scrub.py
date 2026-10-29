# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

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


def test_ordinary_text_is_released_immediately() -> None:
    scrub = RefusalTextScrub()
    scrub.decide("refusal_scrub_hold")
    assert scrub.decide("llm_output", content="我先加载技能。") == "flush_pass"
    assert scrub.armed is False
    assert could_still_be_canned_refusal("作为一个") is True
    assert could_still_be_canned_refusal("口播稿已写完。") is False
