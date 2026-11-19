"""真实SDK Session与序列化快照；每个测试独立checkpointer及故障注入。"""

from types import SimpleNamespace

import pytest
from openjiuwen.core.session.agent import create_agent_session
from openjiuwen.core.session.checkpointer import CheckpointerFactory
from openjiuwen.core.session.checkpointer.inmemory import InMemoryCheckpointer
from openjiuwen.core.single_agent import AgentCard

from jiuwenswarm.server.runtime.skill_turbo import resume_context
from jiuwenswarm.server.runtime.skill_turbo import skill_run_ledger


class SnapshotCheckpointer(InMemoryCheckpointer):
    def __init__(self):
        super().__init__()
        self.reads = 0
        self.writes = 0
        self.mode = ""
        self.fail_next_read = False

    async def pre_agent_execute(self, session, inputs):
        self.reads += 1
        if self.fail_next_read:
            self.fail_next_read = False
            raise OSError("confirmation unavailable")
        await super().pre_agent_execute(session, inputs)

    async def post_agent_execute(self, session):
        self.writes += 1
        if self.mode == "before":
            raise OSError("storage unavailable")
        if self.mode == "drop":
            return
        await super().post_agent_execute(session)
        if self.mode == "after":
            raise OSError("ack lost")
        if self.mode == "confirm":
            self.fail_next_read = True


@pytest.fixture
def storage(monkeypatch):
    cp = SnapshotCheckpointer()
    monkeypatch.setattr(CheckpointerFactory, "_default_checkpointer", cp)
    monkeypatch.setattr(resume_context, "_state_write_locks", {})
    monkeypatch.setattr(resume_context, "_state_callers", {})
    monkeypatch.setattr(resume_context, "_state_keys", {})
    monkeypatch.setattr(skill_run_ledger, "_execution_owners", {})

    def session(sid="segment-session", card_id="segment-agent", *, turbo=True):
        card = AgentCard(id=card_id, name=card_id, description="isolated segment test")
        value = create_agent_session(session_id=sid, card=card)
        if turbo:
            resume_context.set_skill_turbo_id(value, card)
        return value

    return SimpleNamespace(cp=cp, session=session)
