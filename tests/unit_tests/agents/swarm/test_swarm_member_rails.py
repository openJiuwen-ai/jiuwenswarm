# Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

"""Team member heartbeat gating for cron execution sessions."""

from __future__ import annotations

from jiuwenswarm.agents.swarm.context import SwarmBuildContext
from jiuwenswarm.agents.swarm.providers import member_rails


def _context(session_id: str) -> SwarmBuildContext:
    return SwarmBuildContext(
        session_id=session_id,
        channel_id="web",
        user_id="user-1",
        request_metadata={"mode": "team.work.normal"},
        mode="team.work.normal",
        member_card_id="leader-card",
        heartbeat_job_service=object(),
    )


def test_team_heartbeat_rail_skipped_for_persisted_cron_session(monkeypatch) -> None:
    """Web cron 执行会话是普通 ``web_*`` ID，只能靠持久化 cron_id 识别。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {"cron_id": "job-42"},
    )

    assert (
        member_rails._build_heartbeat_rail({}, _context("web_19abc_abcdef123456"))
        is None
    )


def test_team_heartbeat_rail_skipped_for_forwarded_cron_session(monkeypatch) -> None:
    """远端成员只拿到 `cron_session` 时同样不下发心跳工具（无会话元数据）。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {},
    )
    context = SwarmBuildContext(
        session_id="web_19abc_abcdef123456",
        channel_id="default",
        user_id="user-1",
        request_metadata={
            "mode": "team.work.normal",
            "cron_session": {"job_id": "job-42", "source": "session_metadata"},
        },
        mode="team.work.normal",
        member_card_id="calculator-card",
        heartbeat_job_service=object(),
    )

    assert member_rails._build_heartbeat_rail({}, context) is None


def test_team_heartbeat_rail_mounted_for_normal_web_session(monkeypatch) -> None:
    """普通 web team 会话（元数据无 cron_id）仍保留心跳能力。"""
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {"cron_id": ""},
    )

    assert (
        member_rails._build_heartbeat_rail({}, _context("web_19abc_abcdef123456"))
        is not None
    )
