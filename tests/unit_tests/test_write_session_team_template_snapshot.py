# tests/unit_tests/test_write_session_team_template_snapshot.py
# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
"""Tests for the public ``write_session_team_template_snapshot`` writer."""
from __future__ import annotations

from jiuwenswarm.server.runtime.session.session_metadata import (
    get_session_team_template_snapshot,
    write_session_team_template_snapshot,
)

SESSION_ID = "unit_test_session_001"
SNAPSHOT = {
    "team_name": "oc_team_test",
    "team_mode": "predefined",
    "predefined_members": [{"member_name": "office", "display_name": "通用助手"}],
    "agents": {"leader": {"model": {"ref": "model-identity-v1:abc"}}},
}


def test_write_then_read_round_trips(tmp_path):
    write_session_team_template_snapshot(SESSION_ID, SNAPSHOT, sessions_root=tmp_path)
    got = get_session_team_template_snapshot(SESSION_ID, sessions_root=tmp_path)
    assert got == SNAPSHOT


def test_write_overwrites_existing(tmp_path):
    write_session_team_template_snapshot(
        SESSION_ID, {"team_name": "stale"}, sessions_root=tmp_path
    )
    write_session_team_template_snapshot(SESSION_ID, SNAPSHOT, sessions_root=tmp_path)
    got = get_session_team_template_snapshot(SESSION_ID, sessions_root=tmp_path)
    assert got == SNAPSHOT
