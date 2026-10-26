import pytest

from jiuwenswarm.extensions.hook_event import AgentServerHookEvents
from jiuwenswarm.extensions.hooks_context import AgentWsServerStartHookContext
from jiuwenswarm.extensions.registry import ExtensionRegistry


class _RecordingCallbackFramework:
    def __init__(self) -> None:
        self.calls: list[tuple[tuple, dict]] = []

    @staticmethod
    def register_sync(*_args, **_kwargs) -> None:
        return None

    async def trigger(self, *args, **kwargs) -> None:
        self.calls.append((args, kwargs))


def setup_function():
    ExtensionRegistry.reset_instance()


def teardown_function():
    ExtensionRegistry.reset_instance()


def test_agent_ws_server_start_hook_context_to_dict() -> None:
    ctx = AgentWsServerStartHookContext(skills_dir="/tmp/skills")

    assert ctx.skills_dir == "/tmp/skills"
    assert ctx.to_dict() == {"skills_dir": "/tmp/skills"}


@pytest.mark.asyncio
async def test_trigger_fires_agent_server_started_with_skills_dir(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path,
) -> None:
    mod = pytest.importorskip("jiuwenswarm.server.agent_ws_server")
    AgentWebSocketServer = mod.AgentWebSocketServer

    spy = _RecordingCallbackFramework()
    ExtensionRegistry.create_instance(
        callback_framework=spy,
        config={},
        logger=object(),
    )
    skills_dir = tmp_path / "skills"
    monkeypatch.setattr(
        "jiuwenswarm.common.utils.get_agent_skills_dir",
        lambda: skills_dir,
    )

    await AgentWebSocketServer._trigger_agent_server_started_hook()

    assert len(spy.calls) == 1
    args, _kwargs = spy.calls[0]
    assert args[0] == AgentServerHookEvents.AGENT_SERVER_STARTED
    ctx = args[1]
    assert isinstance(ctx, AgentWsServerStartHookContext)
    assert ctx.skills_dir == str(skills_dir)


@pytest.mark.asyncio
async def test_trigger_skips_silently_when_registry_not_initialized(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mod = pytest.importorskip("jiuwenswarm.server.agent_ws_server")
    AgentWebSocketServer = mod.AgentWebSocketServer

    def _boom():
        raise AssertionError("get_agent_skills_dir must not be called when registry is uninitialized")

    monkeypatch.setattr("jiuwenswarm.common.utils.get_agent_skills_dir", _boom)

    await AgentWebSocketServer._trigger_agent_server_started_hook()
