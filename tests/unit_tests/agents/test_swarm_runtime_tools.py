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
