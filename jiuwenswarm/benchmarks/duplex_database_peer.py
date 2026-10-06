"""Official transport inputs enter Jiuwen DB before Native execution."""
import asyncio
import uuid
from pathlib import Path
from types import SimpleNamespace
from functools import wraps

from jiuwenswarm.benchmarks.duplex_runtime import NativePeer


def team_context(method):
    """Bind the SDK's dynamic message-table namespace for every external entry."""
    @wraps(method)
    async def scoped(self, *args, **kwargs):
        from openjiuwen.agent_teams.context import set_session_id, reset_session_id
        token = set_session_id(self.team)
        try:
            return await method(self, *args, **kwargs)
        finally:
            reset_session_id(token)
    return scoped


class DatabasePeer(NativePeer):
    def __init__(self, *, database, team, **kwargs):
        super().__init__(**kwargs)
        self.database, self.team = Path(database), team
        self.db = None
        self._receipts = {}
        self._arrivals = set()
        self._unacknowledged = set()
        self.duplex_settings = {"mode": "active", "policy": self.policy,
                                "model_name": "fast"}

    def has_pending_interrupt(self):
        check = getattr(self.harness, "has_pending_interrupt", None)
        return bool(check()) if check else False

    def has_in_flight_round(self):
        return self.harness.active_round is not None

    async def deliver_input(self, content, *, use_steer=True):
        from openjiuwen.agent_teams.agent.team_agent import TeamAgent
        if self.policy == "abort_restart":
            return await super().receive(str(content), message_id=content.message.message_id)
        phase = self.execution_phase()
        result = await TeamAgent.deliver_input(self, content,
                                              use_steer=use_steer and self.policy != "serial")
        self.events.add("delivery_effective", member=self.name,
            message_id=getattr(getattr(content, "message", None), "message_id", None), phase=phase,
            action="INTERRUPT" if result == "INTERRUPT" else (
                "IDLE_START" if phase == "idle" else "SERIAL" if self.policy == "serial" else "APPEND"))
        return result

    def execution_phase(self):
        active = self.harness.active_round
        return active.iter_phase.value if active is not None else self.harness.state.value

    @team_context
    async def send_to(self, recipient, content):
        """Persist and wake through the SDK; do not wait on the receiver's model."""
        identity = await self.manager.send_message(content=content, to_member_name=recipient)
        if identity is None:
            raise RuntimeError("SDK could not persist the teammate message")
        self.events.add("message_sent", member=self.name, recipient=recipient,
                        message_id=identity, content=content)
        return identity

    @team_context
    async def start(self):
        from filelock import FileLock
        from openjiuwen.agent_teams.messager import InProcessMessager
        from openjiuwen.agent_teams.messager.base import MessagerTransportConfig
        from openjiuwen.agent_teams.tools.database import DatabaseConfig, DatabaseType, TeamDatabase
        from openjiuwen.agent_teams.tools.message_manager import TeamMessageManager
        from openjiuwen.agent_teams.agent.coordination.handlers.message import MessageHandler
        from openjiuwen.agent_teams.agent.coordination.event_bus import EventBus, InnerEventType
        from openjiuwen.agent_teams.schema.events import TeamTopic
        from openjiuwen.agent_teams.schema.team import TeamRole
        from jiuwenswarm.agents.harness.team.duplex_shadow import install_shadow_observer

        install_shadow_observer()
        await super().start()
        if self.db is not None:  # abort reference rebuilds only its executor
            return
        self.database.parent.mkdir(parents=True, exist_ok=True)
        self.db = TeamDatabase(DatabaseConfig(db_type=DatabaseType.SQLITE,
                                             connection_string=str(self.database)))
        lock = FileLock(str(self.database) + ".init.lock", thread_local=False)
        await asyncio.to_thread(lock.acquire, timeout=60)
        try:
            await self.db.initialize()
            # The schema's create methods are idempotent on existing identities.
            if await self.db.team.get_team(self.team) is None:
                await self.db.team.create_team(team_name=self.team, display_name=self.team,
                                              leader_member_name="agent-1")
            if await self.db.member.get_member(self.name, self.team) is None:
                from openjiuwen.core.single_agent.schema.agent_card import AgentCard
                await self.db.member.create_member(member_name=self.name, team_name=self.team,
                    display_name=self.name, status="busy",
                    agent_card=AgentCard(id=self.name, name=self.name).model_dump_json())
        finally:
            await asyncio.to_thread(lock.release)
        # Default node_id is empty: using it for several peers silently
        # replaces the preceding subscriber on the process-global SDK bus.
        self.messager = InProcessMessager(config=MessagerTransportConfig(
            node_id=f"{self.team}/{self.name}", team_name=self.team))
        self.manager = TeamMessageManager(team_name=self.team, db=self.db,
            messager=self.messager, member_name=self.name)
        self.bus = EventBus(role=TeamRole.LEADER)
        blueprint = SimpleNamespace(role=TeamRole.LEADER, member_name=self.name,
                                     language="en", team_spec=None)
        self.handler = MessageHandler(self, blueprint,
            SimpleNamespace(message_manager=self.manager, team_backend=None), SimpleNamespace())
        self.handler._poll = self.bus
        peer = self

        class Dispatch:
            message = peer.handler

            async def dispatch(self, event):
                try:
                    if event.event_type == InnerEventType.POLL_MAILBOX:
                        await peer.handler.on_poll_mailbox(event)
                    else:
                        await peer.handler.on_message_or_broadcast(event)
                    for identity in tuple(peer._unacknowledged):
                        row = await peer.db.message.get_message(identity)
                        if row is not None and row.is_read:
                            peer._unacknowledged.discard(identity)
                            peer.events.add("message_accepted", member=peer.name, message_id=identity)
                            future = peer._receipts.get(identity)
                            if future is not None and not future.done():
                                future.set_result(None)
                except Exception as error:
                    for future in tuple(peer._receipts.values()):
                        if not future.done():
                            future.set_exception(error)
                    raise

        self.topic = TeamTopic.MESSAGE.build(self.team, self.team)
        await self.bus.start(wake_callback=Dispatch().dispatch)
        async def arrived(event):
            payload = event.payload
            if payload.get("to_member_name") == self.name:
                identity = payload["message_id"]
                if identity not in self._arrivals:
                    self._arrivals.add(identity)
                    self._unacknowledged.add(identity)
                    self.events.add("message_arrived", member=self.name, message_id=identity,
                        sender=payload.get("from_member_name"), phase=self.execution_phase())
            await self.bus.enqueue(event)

        await self.messager.subscribe(self.topic, arrived)

    @team_context
    async def receive(self, content, *, message_id, sender="coral"):
        from openjiuwen.agent_teams.schema.events import EventMessage, MessageEvent
        identity = str(uuid.uuid5(uuid.NAMESPACE_URL, f"{self.team}/{self.name}/{message_id}"))
        row = await self.db.message.get_message(identity)
        if row is None:
            created = await self.db.message.create_message(message_id=identity, team_name=self.team,
                from_member_name=sender, to_member_name=self.name, content=content,
                broadcast=False, is_read=False, protocol="plain")
            if not created:
                raise RuntimeError("Jiuwen could not persist the official transport input")
        elif row.content != content or row.from_member_name != sender:
            raise ValueError("external message identity reused with different content")
        elif row.is_read:
            return
        future = self._receipts.setdefault(identity, asyncio.get_running_loop().create_future())
        await self.messager.publish(topic_id=self.topic, message=EventMessage.from_event(MessageEvent(
            message_id=identity, team_name=self.team, from_member_name=sender, to_member_name=self.name)))
        # Only the original event bus / mailbox poll drives the drain.
        try:
            await asyncio.shield(future)
        finally:
            self._receipts.pop(identity, None)

    @team_context
    async def messages(self):
        return await self.db.message.get_team_messages(self.team)

    @team_context
    async def close(self):
        if self.db is not None:
            for future in self._receipts.values():
                if not future.done():
                    future.cancel()
            self._receipts.clear()
            if hasattr(self, "topic"):
                await self.messager.unsubscribe(self.topic)
            if hasattr(self, "bus"):
                await self.bus.stop()
        try:
            await super().close()
        finally:
            if self.db is not None:
                await self.db.close()
                self.db = None
