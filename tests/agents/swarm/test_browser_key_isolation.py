# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Browser identities must remain distinct after the SDK sanitizes them."""

import pytest

from openjiuwen.harness.tools.browser_move.playwright_runtime.config import (
    BrowserInstanceConfig,
)

from jiuwenswarm.agents.swarm.providers.code_subagents import _browser_key


@pytest.mark.parametrize(
    "left,right",
    [
        (("a-b", "c"), ("a", "b-c")),
        (("", "a-b"), ("a", "b")),
        (("session", "a/b"), ("session", "a?b")),
        (("session/a", "member"), ("session?a", "member")),
        (("session", "分析师"), ("session", "审查员")),
        (("", "分析师"), ("", "审查员")),
        (("session", "a.b"), ("session", "a-b")),
    ],
)
def test_distinct_identities_stay_distinct_in_sdk(left, right):
    left_key = _browser_key(*left, "teammate")
    right_key = _browser_key(*right, "teammate")

    assert left_key != right_key
    assert BrowserInstanceConfig(key=left_key).sanitized_key() == left_key
    assert BrowserInstanceConfig(key=right_key).sanitized_key() == right_key


def test_long_identity_produces_bounded_profile_name():
    key = _browser_key("session" * 100, "member" * 100, "teammate")

    assert len(key) == 70
    assert BrowserInstanceConfig(key=key).sanitized_key() == key
    assert key == _browser_key("session" * 100, "member" * 100, "leader")


def test_member_whitespace_normalization_and_role_fallback():
    assert _browser_key("s", " alice ", "leader") == _browser_key(
        "s", "alice", "teammate"
    )
    assert _browser_key("s", "  ", " leader ") == _browser_key("s", "leader", "")


@pytest.mark.parametrize("session_id", ["", "session"])
def test_absent_member_and_role_preserve_legacy_shared_browser(session_id):
    assert _browser_key(session_id, "", "") == ""
    assert _browser_key(session_id, "  ", "  ") == ""
