from __future__ import annotations

import pytest

from jiuwenswarm.runtime.session_delete import SessionDeleteResult
from jiuwenswarm.server.runtime import offline_session_cleanup


@pytest.mark.asyncio
async def test_offline_delete_uses_runtime_with_no_kvc_participants(monkeypatch) -> None:
    captured: dict[str, object] = {}

    class _Runtime:
        def __init__(self, *, participant_registry) -> None:
            captured["registry"] = participant_registry
            self.closed = False

        async def delete_session(self, **kwargs):
            captured["delete"] = kwargs
            return SessionDeleteResult(
                ok=True,
                session_id=kwargs["session_id"],
                deleted=True,
            )

        async def close(self) -> None:
            self.closed = True
            captured["closed"] = True

    monkeypatch.setattr(offline_session_cleanup, "AgentRuntime", _Runtime)

    result = await offline_session_cleanup.delete_offline_session(
        channel_id="web",
        session_id="session-1",
    )

    registry = captured["registry"]
    assert registry.snapshot_activity() == ()
    assert registry.snapshot_delete() == ()
    assert captured["delete"] == {"channel_id": "web", "session_id": "session-1"}
    assert captured["closed"] is True
    assert result.ok is True


@pytest.mark.asyncio
async def test_offline_delete_preserves_success_when_runtime_close_fails(monkeypatch) -> None:
    class _Runtime:
        def __init__(self, *, participant_registry) -> None:
            del participant_registry

        async def delete_session(self, **kwargs):
            return SessionDeleteResult(
                ok=True,
                session_id=kwargs["session_id"],
                deleted=True,
            )

        async def close(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(offline_session_cleanup, "AgentRuntime", _Runtime)

    result = await offline_session_cleanup.delete_offline_session(
        channel_id="web",
        session_id="session-1",
    )

    assert result.ok is True


@pytest.mark.asyncio
async def test_offline_delete_preserves_delete_error_when_runtime_close_fails(monkeypatch) -> None:
    class _Runtime:
        def __init__(self, *, participant_registry) -> None:
            del participant_registry

        async def delete_session(self, **kwargs):
            del kwargs
            raise ValueError("delete failed")

        async def close(self) -> None:
            raise RuntimeError("close failed")

    monkeypatch.setattr(offline_session_cleanup, "AgentRuntime", _Runtime)

    with pytest.raises(ValueError, match="delete failed"):
        await offline_session_cleanup.delete_offline_session(
            channel_id="web",
            session_id="session-1",
        )
