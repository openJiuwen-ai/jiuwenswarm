"""Provider snapshots and identity isolation independent of live model credentials."""

import pytest

from jiuwenswarm.extensions.video_duplex.backend.realtime.sessions import MediaSessions
from jiuwenswarm.extensions.video_duplex.backend import settings
from jiuwenswarm.extensions.video_duplex.backend.openai_realtime_gateway import OpenAIRealtimeConfig


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.setenv("QWEN_OMNI_REALTIME_URL", "wss://workspace.example/realtime")
    monkeypatch.setenv("QWEN_OMNI_API_KEY", "qwen-secret")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "openai-secret")
    monkeypatch.setenv("OPENAI_REALTIME_MODEL", "gpt-realtime-2.1-mini")
    monkeypatch.setenv("OPENAI_REALTIME_VOICE", "marin")
    monkeypatch.setenv("OPENAI_REALTIME_URL", "wss://api.openai.com/v1/realtime")
    return MediaSessions()


def test_snapshot_survives_settings_change(configured, monkeypatch):
    old = configured.create("alice", "task-duplex:one", "openai", "en")
    configured.claim(old.id, old.ticket)
    monkeypatch.setenv("OPENAI_REALTIME_VOICE", "cedar")
    monkeypatch.setenv("OPENAI_REALTIME_API_KEY", "new-secret")
    fresh = configured.create("alice", "task-duplex:one", "openai", "zh-CN")
    assert configured.require(old.id, "alice", "task-duplex:one").config.voice == "marin"
    assert old.config.api_key == "openai-secret"
    assert fresh.config.voice == "cedar"
    assert fresh.config.api_key == "new-secret"


def test_ticket_is_single_use_and_tools_require_active_owned_session(configured):
    session = configured.create("alice", "task-duplex:one", "qwen_omni", "match")
    ticket = session.ticket
    with pytest.raises(ValueError):
        configured.require(session.id, "alice", "task-duplex:one")
    with pytest.raises(ValueError):
        configured.claim(session.id, "wrong")
    configured.claim(session.id, ticket)
    for owner, conversation in [("bob", "task-duplex:one"), ("alice", "task-duplex:two")]:
        with pytest.raises(ValueError):
            configured.require(session.id, owner, conversation)
    with pytest.raises(ValueError):
        configured.claim(session.id, ticket)
    configured.close(session.id)
    with pytest.raises(ValueError):
        configured.require(session.id, "alice", "task-duplex:one")


def test_pending_ticket_expires(configured):
    session = configured.create("", "task-duplex:one", "qwen_omni", "match")
    session.expires_at = 0
    with pytest.raises(ValueError):
        configured.claim(session.id, session.ticket)
    assert not configured.sessions


def test_capabilities_have_provider_tool_shapes_and_no_credentials(configured):
    for provider in ("qwen_omni", "openai"):
        session = configured.create("alice", "task-duplex:one", provider, "en")
        payload = configured.payload(session)
        assert "secret" not in str(payload)
        assert "upstream_url" not in payload
        assert payload["url"].startswith("/ws/video/realtime?")
        assert payload["tools"]
        tool = payload["tools"][0]
        assert ("function" in tool) == (provider == "qwen_omni")
        assert ("name" in tool) == (provider == "openai")


def test_openai_url_validation_and_model(configured, monkeypatch):
    config = OpenAIRealtimeConfig.from_environment()
    assert config.upstream_with_model().endswith("?model=gpt-realtime-2.1-mini")
    for url in ("http://example", "ws://example", "wss://user:pass@example", "wss://example/#token"):
        monkeypatch.setenv("OPENAI_REALTIME_URL", url)
        with pytest.raises(ValueError):
            OpenAIRealtimeConfig.from_environment().validate()


def test_openai_settings_preserve_other_provider_and_redact_key(configured, monkeypatch):
    persisted = {}
    monkeypatch.setattr(settings, "_persist_env_updates", persisted.update)
    settings.update_settings({"video_live_provider": "openai", "openai_realtime_model": "gpt-realtime-2.1-mini"})
    assert settings._provider() == "openai"
    assert persisted["VIDEO_REALTIME_PROVIDER"] == "openai"
    payload = settings.settings_payload(enabled=True)
    assert payload["values"]["openai_realtime_api_key"] == ""
    assert payload["configured_secret_lengths"]["openai_realtime_api_key"] == len("openai-secret")
    assert payload["configured_secret_lengths"]["qwen_omni_api_key"] == len("qwen-secret")


def test_legacy_realtime_mode_defaults_to_qwen(monkeypatch):
    monkeypatch.setenv("VIDEO_LIVE_MODE", "realtime")
    monkeypatch.delenv("VIDEO_REALTIME_PROVIDER", raising=False)
    assert settings._provider() == "qwen_omni"
