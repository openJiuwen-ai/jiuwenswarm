from types import SimpleNamespace

from jiuwenswarm.agents.swarm.providers import runtime_tools


def test_team_cron_tools_inherit_project_binding_from_session(monkeypatch) -> None:
    """Code-team cron creation must retain the caller's project binding."""
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {
            "project_id": "proj-code",
            "project_dir": r"D:\workspace\code-project",
            "work_mode": "code",
            "model": "chat-model",
        },
    )

    def build_tools(_self, *, context, **_kwargs):
        captured["metadata"] = context.metadata
        captured["mode"] = context.mode
        return []

    monkeypatch.setattr(runtime_tools.CronRuntimeBridge, "build_tools", build_tools)
    context = SimpleNamespace(
        member_card_id="team-leader",
        channel_id="web",
        session_id="web-code-session",
        request_metadata={"request_id": "req-code"},
        user_id="",
        language="cn",
        mode="code.team",
    )

    assert runtime_tools.build_cron_tools({}, context) == []
    assert captured["metadata"] == {
        "request_id": "req-code",
        "project_id": "proj-code",
        "project_dir": r"D:\workspace\code-project",
        "work_mode": "code",
        "model_name": "chat-model",
    }
    assert captured["mode"] == "code.team"


def test_team_cron_tools_blocked_inside_cron_execution_session(monkeypatch) -> None:
    """Cron execution sessions get no cron toolkit at all (no nested cron).

    Cluster remote members rely on the scheduler's cron marker and the
    ``__cron___`` session-id prefix: their channel is ``default`` and their
    rebuilt request metadata only carries what the bootstrap envelope
    forwarded, so both signals must gate the toolkit (issue #5116).
    """

    def build_tools(_self, *, context, **_kwargs):
        raise AssertionError("cron tools must not be built for a cron execution session")

    monkeypatch.setattr(runtime_tools.CronRuntimeBridge, "build_tools", build_tools)
    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {},
    )

    def _context(**overrides):
        values = dict(
            member_card_id="teammate-1",
            channel_id="default",
            session_id="web-session",
            request_metadata={"mode": "team"},
            user_id="",
            language="cn",
            mode="team",
        )
        values.update(overrides)
        return SimpleNamespace(**values)

    # Signal 1: the scheduler's cron marker in request metadata.
    assert (
        runtime_tools.build_cron_tools(
            {},
            _context(
                session_id="__cron___19abc_rand12",
                request_metadata={"mode": "team", "cron": {"job_id": "job-1"}},
            ),
        )
        == []
    )
    # Signal 2: the internal __cron__ channel.
    assert (
        runtime_tools.build_cron_tools(
            {},
            _context(channel_id="__cron__", request_metadata={}),
        )
        == []
    )
    # Signal 3: the cron/__cron__ session-id prefix (remote members of a team
    # cron run execute in a __cron___<ts>_<rand> session).
    assert (
        runtime_tools.build_cron_tools(
            {},
            _context(session_id="__cron___19abc_rand12"),
        )
        == []
    )
    assert runtime_tools.build_cron_tools({}, _context(session_id="cron_job1")) == []
