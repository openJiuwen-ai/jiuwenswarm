# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""``slack`` is a cron delivery target on every surface that lists one.

The repository ships a Slack connector and ``ChannelType`` declares ``slack``,
so a job may name it. These tests cover the surfaces a target value has to
reach to be usable: the enum, the store round-trip, the ``CronController`` gate
behind the web and TUI RPCs, the tool schemas the model reads, and the default
target of a job scheduled inside a Slack turn. Delivery is covered by
``test_slack_push_addresses_the_slack_channel`` in ``test_cron_scheduler.py``.

What happens to a value no connector serves is a separate question. These
tests only pin that ``slack`` is no longer one of those values.
"""

from __future__ import annotations

import pytest

from jiuwenswarm.agents.harness.common.tools.cron.cron_tools import CronTools
from jiuwenswarm.gateway.cron.controller import CronController
from jiuwenswarm.gateway.cron.models import (
    CronTargetChannel,
    is_valid_target_channel_id,
    normalize_target_channel_id,
)
from jiuwenswarm.gateway.cron.store import CronJobStore


def test_slack_is_a_declared_cron_target() -> None:
    assert CronTargetChannel.SLACK.value == "slack"
    assert is_valid_target_channel_id("slack")
    assert is_valid_target_channel_id("SLACK")
    assert normalize_target_channel_id("SLACK") == "slack"


def test_an_unknown_target_is_still_not_a_declared_one() -> None:
    assert not is_valid_target_channel_id("not-a-channel")


@pytest.mark.asyncio
async def test_a_stored_slack_job_reloads_as_slack(tmp_path) -> None:
    """``targets`` survives the ``CronJob.from_dict`` round-trip on reload.

    Without the enum member the created job reported ``slack``, the file held
    ``slack``, and the next load returned ``web``.
    """
    path = tmp_path / "cron_jobs.json"
    created = await CronJobStore(path=path).create_job(
        name="daily digest",
        cron_expr="0 0 9 * * ? *",
        timezone="Asia/Shanghai",
        description="post the digest",
        targets="slack",
        session_id="slack_T1_C1_U1",
    )
    assert created.targets == "slack"

    reloaded = await CronJobStore(path=path).get_job(created.id)
    assert reloaded is not None
    assert reloaded.targets == "slack"


def test_the_controller_accepts_slack_as_targets() -> None:
    controller = CronController.__new__(CronController)
    controller._target_channel = None
    assert controller._normalize_targets("slack") == "slack"


def test_the_controller_still_names_every_target_it_accepts() -> None:
    """The rejection text is hand-written, so it has to list the enum."""
    controller = CronController.__new__(CronController)
    controller._target_channel = None
    with pytest.raises(ValueError) as excinfo:
        controller._normalize_targets("not-a-channel")
    message = str(excinfo.value)
    for channel in CronTargetChannel:
        assert channel.value in message, message


@pytest.mark.parametrize("owner", [CronController, CronTools])
def test_the_tool_schemas_offer_slack(owner) -> None:
    """Every ``targets`` schema the model reads lists the accepted values."""
    enums = [
        prop["enum"]
        for tool in owner.__new__(owner).get_tools()
        for prop in _targets_properties(tool.card.input_params)
        if isinstance(prop.get("enum"), list)
    ]
    assert enums, f"no targets schema of {owner.__name__} declares an enum"
    for values in enums:
        assert "slack" in values


def _targets_properties(schema: dict) -> list[dict]:
    """Collect every ``targets`` property in a tool schema, at any depth."""
    found: list[dict] = []
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return found
    for name, prop in properties.items():
        if not isinstance(prop, dict):
            continue
        if name == "targets":
            found.append(prop)
        found.extend(_targets_properties(prop))
    return found


def test_a_slack_turn_defaults_its_jobs_to_slack() -> None:
    """A job the agent creates inside a Slack conversation stays in Slack.

    ``_default_target_from_channel`` returns ``web`` for a channel it has no
    branch for, so a Slack turn used to schedule its own result away from the
    conversation that asked for it.
    """
    tools = CronTools.__new__(CronTools)
    tools._route = lambda: _SlackRoute()
    assert tools._default_target_from_channel() == "slack"
    assert tools._normalize_targets_param("") == "slack"
    assert tools._normalize_targets_param("slack") == "slack"


class _SlackRoute:
    """Minimal routing context for a request arriving over Slack."""

    channel_id = "slack"
    session_id = "slack_T1_C1_U1"
    request_id = ""
    chat_type = None
