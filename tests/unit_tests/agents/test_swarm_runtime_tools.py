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
<<<<<<< HEAD


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
||||||| e487e9a30
=======


def _cron_tool_sentinel() -> list[SimpleNamespace]:
    """A non-empty toolkit with the tool whose creation must stay blocked."""
    return [SimpleNamespace(card=SimpleNamespace(name="cron_create_job"))]


def test_team_cron_tools_blocked_inside_cron_execution_session(monkeypatch) -> None:
    """Cron execution sessions get no cron toolkit at all (no nested cron).

    Cluster remote members rely on the scheduler's cron marker and the
    ``__cron___`` session-id prefix: their channel is ``default`` and their
    rebuilt request metadata only carries what the bootstrap envelope
    forwarded, so both signals must gate the toolkit (issue #5116).
    """
    built_contexts: list[object] = []

    def build_tools(_self, *, context, **_kwargs):
        # Return a real toolkit instead of raising: the provider catches
        # exceptions and turns them into ``[]``, which would make an
        # "assert == []" case pass even when the gate is broken.
        built_contexts.append(context)
        return _cron_tool_sentinel()

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

    contexts = [
        # Signal 1: the scheduler's cron marker in request metadata.
        _context(
            session_id="__cron___19abc_rand12",
            request_metadata={"mode": "team", "cron": {"job_id": "job-1"}},
        ),
        # Signal 2: the internal __cron__ channel.
        _context(channel_id="__cron__", request_metadata={}),
        # Signal 3: the cron/__cron__ session-id prefix (remote members of a
        # team cron run execute in a __cron___<ts>_<rand> session).
        _context(session_id="__cron___19abc_rand12"),
        _context(session_id="cron_job1"),
        # Signal 4: session-level cron ownership forwarded across the bootstrap
        # boundary for a Web cron session's later turn (no scheduler marker,
        # remote node without the session metadata).
        _context(
            channel_id="default",
            request_metadata={
                "mode": "team",
                "cron_session": {"job_id": "job-42", "source": "session_metadata"},
            },
        ),
    ]
    for context in contexts:
        assert runtime_tools.build_cron_tools({}, context) == []
    assert built_contexts == []


def test_team_cron_tools_blocked_for_persisted_cron_session(monkeypatch) -> None:
    """Web cron execution sessions are ordinary ``web_*`` ids on the web channel.

    Only the persisted ``cron_id`` identifies them once the scheduler request is
    gone (user follow-up, rebuilt runtime), so the provider must consult it just
    like the single-agent adapter does (issue #6188).
    """
    built_contexts: list[object] = []

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {"cron_id": "job-42", "project_id": "proj-1"},
    )

    def build_tools(_self, *, context, **_kwargs):
        built_contexts.append(context)
        return _cron_tool_sentinel()

    monkeypatch.setattr(runtime_tools.CronRuntimeBridge, "build_tools", build_tools)
    context = SimpleNamespace(
        member_card_id="team-leader",
        channel_id="web",
        session_id="web_19abc_abcdef123456",
        request_metadata={"mode": "team"},
        user_id="user-1",
        language="cn",
        mode="team",
    )

    assert runtime_tools.build_cron_tools({}, context) == []
    assert built_contexts == []


def test_team_cron_tools_built_for_normal_web_session(monkeypatch) -> None:
    """A normal web team session keeps its cron toolkit after the change."""
    expected = _cron_tool_sentinel()

    monkeypatch.setattr(
        "jiuwenswarm.server.runtime.session.session_metadata.get_session_metadata",
        lambda *_args, **_kwargs: {"cron_id": "", "project_id": "proj-1"},
    )

    def build_tools(_self, *, context, **_kwargs):
        _ = context
        return expected

    monkeypatch.setattr(runtime_tools.CronRuntimeBridge, "build_tools", build_tools)
    context = SimpleNamespace(
        member_card_id="team-leader",
        channel_id="web",
        session_id="web_19abc_abcdef123456",
        request_metadata={"mode": "team"},
        user_id="user-1",
        language="cn",
        mode="team",
    )

    built = runtime_tools.build_cron_tools({}, context)
    assert [tool.card.name for tool in built] == ["cron_create_job"]
>>>>>>> origin/develop
