# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""The unit-test checkpointer stays usable after another test replaces it."""

from openjiuwen.core.session.checkpointer import CheckpointerFactory

from tests.unit_tests.conftest import _ensure_agent_checkpointer


def test_bare_checkpointer_is_replaced_before_the_next_session() -> None:
    original = CheckpointerFactory.get_checkpointer()
    CheckpointerFactory.set_default_checkpointer(object())
    try:
        _ensure_agent_checkpointer()
        current = CheckpointerFactory.get_checkpointer()
        assert hasattr(current, "pre_agent_execute")
        assert hasattr(current, "pre_agent_team_execute")
        assert hasattr(current, "release")
    finally:
        CheckpointerFactory.set_default_checkpointer(original)
