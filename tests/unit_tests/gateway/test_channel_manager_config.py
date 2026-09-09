"""Regression coverage for atomic channel configuration updates."""

from __future__ import annotations

import pytest

from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager


class _MessageHandler:
    pass


async def test_set_conf_restores_last_working_config_when_callback_fails() -> None:
    async def reject_bad_config(config: dict) -> None:
        if config.get("bad"):
            raise RuntimeError("optional channel unavailable")

    manager = ChannelManager(
        _MessageHandler(),
        config={"working": {"enabled": True}},
        on_config_updated=reject_bad_config,
    )

    with pytest.raises(RuntimeError, match="optional channel unavailable"):
        await manager.set_conf("bad", {"enabled": True})

    assert manager.get_conf("working") == {"enabled": True}
    assert manager.get_conf("bad") == {}
    # The failed write is still observable to a background retry.  Otherwise a
    # user disabling the channel while that retry sleeps is indistinguishable
    # from the retry's own rollback-to-empty state.
    assert manager.get_conf_revision("bad") == 1


async def test_set_config_restores_last_working_snapshot_when_callback_fails() -> None:
    async def reject_new_config(config: dict) -> None:
        if "bad" in config:
            raise RuntimeError("optional channel unavailable")

    manager = ChannelManager(
        _MessageHandler(),
        config={"working": {"enabled": True}},
        on_config_updated=reject_new_config,
    )

    with pytest.raises(RuntimeError, match="optional channel unavailable"):
        await manager.set_config({"bad": {"enabled": True}})

    assert manager.get_conf("working") == {"enabled": True}
    assert manager.get_conf("bad") == {}
    assert manager.get_conf_revision("bad") == 1


async def test_config_revision_changes_when_a_user_disables_a_channel() -> None:
    manager = ChannelManager(_MessageHandler(), config={"telegram": {"enabled": True}})

    initial_revision = manager.get_conf_revision("telegram")
    await manager.set_conf("telegram", {})

    assert manager.get_conf("telegram") == {}
    assert manager.get_conf_revision("telegram") == initial_revision + 1
