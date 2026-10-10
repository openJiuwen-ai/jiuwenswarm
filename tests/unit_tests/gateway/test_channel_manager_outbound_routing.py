"""Which instance an outbound message reaches, and what a key collision says.

Two things are pinned here.  That a message reaches the account it belongs to
rather than whichever instance the index happened to scan first.  And that a
registration displacing another under one ``ChannelKey`` is reported instead
of swallowed.

The deployments that must not change are pinned as well: a single instance,
and a platform whose instances implement no ``claims_message`` hook.
"""

from __future__ import annotations

import asyncio
import logging
import time

import pytest

from jiuwenswarm.common.schema.message import EventType, Message
from jiuwenswarm.gateway.channel_manager.channel_manager import ChannelManager
from jiuwenswarm.gateway.routing.keys import ChannelKey

LOGGER_NAME = "jiuwenswarm.gateway.channel_manager.channel_manager"
_DROP_LINE = "无法判定出站消息归属于"


class _FakeMessageHandler:
    """The MessageHandler accessors these paths reach for."""

    def __init__(self) -> None:
        self.agent_client = None
        self.queue: asyncio.Queue = asyncio.Queue()

    @staticmethod
    def resolve_app_id(msg: Message) -> str:
        return getattr(msg, "app_id", None) or getattr(msg, "bot_id", None) or "default"

    async def consume_robot_messages(self, timeout: float | None = None):
        try:
            return await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None


class _Plain:
    """A channel whose instances cannot tell their own messages apart."""

    def __init__(self, app_id: str, channel_id: str = "plain") -> None:
        self.channel_id = channel_id
        self.app_id = app_id
        self.sent: list[Message] = []

    def on_message(self, callback) -> None:
        pass

    async def send(self, msg: Message, **_kw) -> None:
        self.sent.append(msg)


class _Owned(_Plain):
    """A channel that knows which account it posts as."""

    def __init__(self, app_id: str, owner: str, channel_id: str = "acct") -> None:
        super().__init__(app_id, channel_id=channel_id)
        self.owner = owner

    def claims_message(self, msg: Message) -> bool:
        return (msg.metadata or {}).get("owner") == self.owner


def _manager() -> tuple[ChannelManager, _FakeMessageHandler]:
    handler = _FakeMessageHandler()
    return ChannelManager(handler), handler


def _reply(*, owner: str = "", app_id: str | None = None, channel_id: str = "acct") -> Message:
    return Message(
        id="reply-1",
        type="event",
        channel_id=channel_id,
        session_id="s-1",
        params={},
        timestamp=time.time(),
        ok=True,
        payload={"event_type": "chat.final", "content": "hello"},
        event_type=EventType.CHAT_FINAL,
        metadata={"owner": owner} if owner else {},
        app_id=app_id,
    )


def _drops(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if _DROP_LINE in r.getMessage()]


# ---------- rung 1: the exact key ----------


def test_the_exact_key_wins_before_anyone_is_asked() -> None:
    """app_id on the message is the precise route; a claim is only the fallback."""
    manager, _ = _manager()
    one = _Owned("app-one", "org-one")
    two = _Owned("app-two", "org-two")
    manager.register_channel(one)
    manager.register_channel(two)

    # The key names app-two while the metadata names org-one: the key decides.
    assert manager._resolve_outbound_channel(_reply(owner="org-one", app_id="app-two")) is two


# ---------- rung 2: the only instance ----------


def test_one_instance_is_never_asked_who_owns_a_message() -> None:
    """A single-account deployment keeps delivering a message that names nobody."""
    manager, _ = _manager()
    only = _Owned("app-one", "org-one")
    manager.register_channel(only)

    assert manager._resolve_outbound_channel(_reply()) is only
    assert manager._resolve_outbound_channel(_reply(owner="org-elsewhere")) is only


# ---------- rung 3: the claim ----------


def test_the_one_instance_that_claims_the_message_gets_it() -> None:
    manager, _ = _manager()
    one = _Owned("app-one", "org-one")
    two = _Owned("app-two", "org-two")
    manager.register_channel(one)
    manager.register_channel(two)

    assert manager._resolve_outbound_channel(_reply(owner="org-two")) is two
    assert manager._resolve_outbound_channel(_reply(owner="org-one")) is one


def test_an_unattributable_message_is_refused_rather_than_guessed(caplog) -> None:
    """Returning the first instance would post one account's reply in another."""
    manager, _ = _manager()
    manager.register_channel(_Owned("app-one", "org-one"))
    manager.register_channel(_Owned("app-two", "org-two"))

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert manager._resolve_outbound_channel(_reply()) is None
        # An account nobody serves is equally unattributable.
        assert manager._resolve_outbound_channel(_reply(owner="org-third")) is None

    assert "acct" in caplog.text


def test_the_refusal_is_warned_once_not_once_per_message(caplog) -> None:
    """The producers that reach this rung arrive on a timer.

    A health-check relay and a scheduled push name no account, so an unlatched
    warning repeats for as long as the process runs.  The condition is a
    standing property of the configuration and is worth one line.
    """
    manager, _ = _manager()
    manager.register_channel(_Owned("app-one", "org-one"))
    manager.register_channel(_Owned("app-two", "org-two"))

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        for _ in range(5):
            assert manager._resolve_outbound_channel(_reply()) is None

    dropped = _drops(caplog)
    assert len(dropped) == 5, "every drop stays traceable"
    assert dropped[0].levelno == logging.WARNING
    assert all(r.levelno == logging.DEBUG for r in dropped[1:])


def test_the_latch_is_held_per_channel_id_not_process_wide(caplog) -> None:
    """One platform's standing condition must not silence another's first report."""
    manager, _ = _manager()
    manager.register_channel(_Owned("app-one", "org-one"))
    manager.register_channel(_Owned("app-two", "org-two"))
    for app_id in ("app-three", "app-four"):
        manager.register_channel(_Owned(app_id, f"org-{app_id}", channel_id="other"))
    manager._resolve_outbound_channel(_reply())

    with caplog.at_level(logging.DEBUG, logger=LOGGER_NAME):
        caplog.clear()   # drop the priming call's own report
        assert manager._resolve_outbound_channel(_reply()) is None
        assert manager._resolve_outbound_channel(_reply(channel_id="other")) is None

    warned = [r.getMessage() for r in _drops(caplog) if r.levelno == logging.WARNING]
    assert len(warned) == 1, "acct was already reported, other was not"
    assert "other" in warned[0]


# ---------- the platforms the strict rule must not change ----------


def test_a_platform_that_cannot_say_keeps_its_first_match() -> None:
    """Refusing here would break multi-app Feishu, which implements no hook."""
    manager, _ = _manager()
    first = _Plain("cli_one")
    manager.register_channel(first)
    manager.register_channel(_Plain("cli_two"))

    assert manager._resolve_outbound_channel(_reply(channel_id="plain")) is first


def test_a_channel_id_with_no_instance_still_resolves_to_nothing(caplog) -> None:
    manager, _ = _manager()
    manager.register_channel(_Plain("cli_one"))

    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        assert manager._resolve_outbound_channel(_reply(channel_id="absent")) is None

    assert "未找到 Channel" in caplog.text


def test_a_hook_that_raises_answers_not_mine(caplog) -> None:
    """One instance's bad answer must not stop the dispatch queue."""

    class _Broken(_Owned):
        def claims_message(self, msg: Message) -> bool:
            raise RuntimeError("the connection has not identified itself yet")

    manager, _ = _manager()
    broken = _Broken("app-one", "org-one")
    good = _Owned("app-two", "org-two")
    manager.register_channel(broken)
    manager.register_channel(good)

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        assert manager._resolve_outbound_channel(_reply(owner="org-two")) is good

    assert "claims_message" in caplog.text


# ---------- the dispatch loop uses the resolution ----------


@pytest.mark.parametrize(
    ("owner", "delivered"),
    [("org-two", 1), ("", 0)],
)
async def test_the_dispatch_loop_delivers_what_the_resolution_returns(
    owner: str, delivered: int
) -> None:
    """Pins the wiring: a refusal must stop the send, not fall back to a guess."""
    manager, handler = _manager()
    one = _Owned("app-one", "org-one")
    two = _Owned("app-two", "org-two")
    manager.register_channel(one)
    manager.register_channel(two)

    await manager.start_dispatch()
    handler.queue.put_nowait(_reply(owner=owner))
    for _ in range(200):
        await asyncio.sleep(0.005)
        if handler.queue.empty():
            break
    await asyncio.sleep(0.05)
    await manager.stop_dispatch()

    assert one.sent == []
    assert len(two.sent) == delivered


# ---------- a registration that displaces another ----------


def test_a_displacing_registration_is_reported_rather_than_swallowed(caplog) -> None:
    """The replaced instance keeps its connection and stops receiving replies."""
    manager, _ = _manager()
    first = _Plain("cli_one")
    second = _Plain("cli_one")

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        manager.register_channel(first)
        manager.register_channel(second)

    assert [r.levelno for r in caplog.records] == [logging.ERROR]
    assert "ChannelKey" in caplog.text
    # The newcomer still wins, so a stop path that failed to unregister cannot
    # strand a dead channel in the map for good.
    assert manager.get_by_key(ChannelKey("plain", "cli_one")) is second


def test_registering_the_same_channel_twice_is_not_a_displacement(caplog) -> None:
    manager, _ = _manager()
    channel = _Plain("cli_one")

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        manager.register_channel(channel)
        manager.register_channel(channel)

    assert caplog.records == []


def test_two_instances_with_distinct_app_ids_both_register(caplog) -> None:
    manager, _ = _manager()
    first = _Plain("cli_one")
    second = _Plain("cli_two")

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        manager.register_channel(first)
        manager.register_channel(second)

    assert caplog.records == []
    assert set(manager._channels) == {
        ChannelKey("plain", "cli_one"),
        ChannelKey("plain", "cli_two"),
    }


def test_the_other_registration_paths_report_a_displacement_too(caplog) -> None:
    """``register_channel`` has two siblings that write the same table."""
    manager, _ = _manager()

    with caplog.at_level(logging.ERROR, logger=LOGGER_NAME):
        manager.register_channel_with_inbound(_Plain("cli_one"), lambda _msg: None)
        manager.register_channel_with_inbound(_Plain("cli_one"), lambda _msg: None)
        manager.register_external_channel(ChannelKey("plain", "cli_two"), _Plain("cli_two"))
        manager.register_external_channel(ChannelKey("plain", "cli_two"), _Plain("cli_two"))

    assert len(caplog.records) == 2
    assert all(r.levelno == logging.ERROR for r in caplog.records)
