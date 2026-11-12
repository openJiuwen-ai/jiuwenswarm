from types import SimpleNamespace

from jiuwenswarm.agents.swarm.providers import runtime_tools


def _patch(monkeypatch, session_metadata: dict) -> None:
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: session_metadata,
    )
    monkeypatch.setattr(
        runtime_tools.CronRuntimeBridge,
        "build_tools",
        lambda _self, **_kwargs: ["cron_tool"],
    )


def _context(session_id: str, channel_id: str = "web", request_metadata: dict | None = None):
    return SimpleNamespace(
        member_card_id="team-leader",
        channel_id=channel_id,
        session_id=session_id,
        request_metadata=request_metadata or {},
        user_id="",
        language="cn",
        mode="team",
    )


def test_cron_tools_withheld_for_cron_prefixed_session(monkeypatch) -> None:
    _patch(monkeypatch, {})
    assert runtime_tools.build_cron_tools({}, _context("cron_123_job1")) == []


def test_cron_tools_withheld_when_request_metadata_marks_cron(monkeypatch) -> None:
    _patch(monkeypatch, {})
    ctx = _context("sess_abc", request_metadata={"cron": {"job_id": "job1"}})
    assert runtime_tools.build_cron_tools({}, ctx) == []


def test_cron_tools_withheld_when_session_metadata_has_cron_id(monkeypatch) -> None:
    """Regression: team cron sessions are allocated via session.create.

    The id is not ``cron_*`` and the channel is the real target channel, so the
    only durable marker is the persisted ``cron_id`` (as the single-agent path
    already checks). Without it a cron run can create nested cron jobs.
    """
    _patch(monkeypatch, {"cron_id": "job1"})
    assert runtime_tools.build_cron_tools({}, _context("sess_abc")) == []


def test_cron_tools_kept_for_normal_session(monkeypatch) -> None:
    _patch(monkeypatch, {})
    assert runtime_tools.build_cron_tools({}, _context("sess_abc")) == ["cron_tool"]
