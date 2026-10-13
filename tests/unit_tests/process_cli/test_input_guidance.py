# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

from types import SimpleNamespace

import pytest

from openjiuwen.harness.prompts import SystemPromptBuilder
from jiuwenswarm.agents.harness.common.prompt.prompt_builder import _input_prompt
from jiuwenswarm.agents.harness.common.rails.response_prompt_rail import (
    ResponsePromptRail,
)


@pytest.mark.parametrize("language", ["cn", "en"])
@pytest.mark.asyncio
async def test_cli_input_guidance_recognizes_actual_task_envelope(language):
    builder = SystemPromptBuilder(language=language)
    rail = ResponsePromptRail()
    rail.init(SimpleNamespace(system_prompt_builder=builder))
    rail.set_channel("process_cli")
    await rail.before_model_call(SimpleNamespace(inputs=None, extra={}))
    text = builder.build()
    assert 'source: "process_cli"' in text
    assert 'type: "user input"' in text
    assert "`content`" in text
    assert "<system-reminder>" in text


@pytest.mark.parametrize(
    "channel", [None, "web", "tui", "feishu", "agent_session", "cron"]
)
@pytest.mark.parametrize("language", ["cn", "en"])
def test_other_channels_keep_existing_input_instructions(channel, language):
    baseline = _input_prompt(language)
    actual = _input_prompt(language, channel=channel)
    assert actual.content == baseline.content
