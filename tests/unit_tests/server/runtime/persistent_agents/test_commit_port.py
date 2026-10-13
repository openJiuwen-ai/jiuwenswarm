"""SW-04: CommitPort event log, critical durability and candidate outputs."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from jiuwenswarm.server.runtime.persistent_agents.execution_models import (
    AccessSnapshot,
    ConversationRef,
    build_execution_context,
)
from jiuwenswarm.server.runtime.persistent_agents.execution_repository import ExecutionRepository
from jiuwenswarm.server.runtime.persistent_agents.models import PersistentAgentError
from jiuwenswarm.server.runtime.persistent_agents.ports.commit import CommitQueue, HostCommitPort
from jiuwenswarm.server.runtime.persistent_agents.spec_provider import publish_spec

from openjiuwen.core.common.exception.errors import BaseError
from openjiuwen.core.foundation.llm import ModelClientConfig, ModelRequestConfig
from openjiuwen.core.foundation.store import InMemoryKVStore
from openjiuwen.harness import persistent_agent as core
from openjiuwen.harness.schema.deep_agent_spec import TeamModelConfig

NS = "workspace-1"
AGENT = "mate_dev"
NOW = datetime(2026, 10, 13, 9, 0, tzinfo=timezone.utc)
ACCESS = AccessSnapshot(
    principal_ref="user-1",
    startup_scope=frozenset({"chat"}),
    source_scope=frozenset({"chat"}),
    policy_revision="policy-1",
)
SNAPSHOT = publish_spec(
    AGENT,
    "v1",
    {
        "card": {"id": AGENT, "name": "研发同事", "description": "研发"},
        "system_prompt": "你是研发同事。",
        "model": TeamModelConfig(
            model_client_config=ModelClientConfig(
                client_id=f"jiuwenmate:{AGENT}",
                client_provider="OpenAI",
                api_key="sk-test",
                api_base="https://example.invalid/v1",
                model_name="deepseek",
            ),
            model_request_config=ModelRequestConfig(model="deepseek"),
        ),
    },
)


def accept_and_start(repository: ExecutionRepository, conversation_key: str, source_ref: str):
    session = repository.resolve_session(NS, AGENT, ConversationRef.web(conversation_key), now=NOW)
    state, _ = repository.accept_request(
        session,
        snapshot=SNAPSHOT,
        source_ref=source_ref,
        access=ACCESS,
        request_input=core.RequestInput(kind="scheduled", text="整理进展", reply_target="reply-1"),
        received_at=NOW,
    )
    execution = repository.start_execution(NS, state.record.request_id, now=NOW)
    return build_execution_context(state.record, execution, capability_snapshot=SNAPSHOT.enabled_features)


def progress(execution, seq, text="正在整理"):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.PROGRESS_UPDATED,
        local_seq=seq,
        occurred_at=NOW,
        visibility="progress",
        payload={"text": text},
    )


def candidate(execution, seq, output_id="out-1", text="进展如下"):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.OUTPUT_PROPOSED,
        local_seq=seq,
        occurred_at=NOW,
        visibility="candidate",
        payload={"output_id": output_id, "target_ref": "reply-1", "share_mode": "message", "text": text},
    )


def final(execution, seq, output_refs=("out-1",)):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_FINAL,
        local_seq=seq,
        occurred_at=NOW,
        payload={"status": "completed", "output_refs": output_refs},
    )


def paused(execution, seq):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_PAUSED,
        local_seq=seq,
        occurred_at=NOW,
        payload={"reason": "stopped"},
    )


def tool_intent(execution, seq):
    return core.AgentEvent.create(
        execution,
        event_type=core.EventType.TOOL_INTENT,
        local_seq=seq,
        occurred_at=NOW,
        payload={
            "operation_id": "op-1",
            "tool_call_id": "call-1",
            "tool_name": "write_file",
            "params_digest": "a" * 64,
            "args_summary": "写入 report.md",
        },
    )


class FlakyRepository(ExecutionRepository):
    """Fails writes of the listed event types."""

    def __init__(self, db_path, failing=()):
        super().__init__(db_path)
        self.failing = set(failing)

    def record_events(self, events, *, now):
        if any(event.event_type in self.failing for event in events):
            raise OSError("disk unavailable")
        super().record_events(events, now=now)


@pytest.fixture
async def host(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    yield repository, queue, HostCommitPort(repository, queue)
    await queue.aclose()


async def test_ordinary_events_are_queued_and_read_back_in_order(host):
    repository, queue, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, progress(execution, 2, "第二步"))
    await port.record(execution, progress(execution, 1, "第一步"))
    await queue.drain()

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.local_seq for event in log] == [1, 2]
    assert log[0].payload.text == "第一步"


async def test_resubmitted_event_id_is_stored_once(host):
    repository, queue, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    event = progress(execution, 1)

    await port.record(execution, event)
    await port.record(execution, event)
    await queue.drain()
    await port.record(execution, tool_intent(execution, 2))
    await port.record(execution, tool_intent(execution, 2))

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [item.event_id for item in log] == [event.event_id, tool_intent(execution, 2).event_id]


async def test_same_event_id_with_other_content_is_rejected(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, paused(execution, 1))

    other = core.AgentEvent.create(
        execution,
        event_type=core.EventType.EXECUTION_PAUSED,
        local_seq=1,
        occurred_at=NOW,
        payload={"reason": "error", "detail": "模型异常"},
    )
    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, other)
    assert caught.value.code == "CONFLICT"


async def test_failed_ordinary_write_does_not_block_a_later_critical_event(tmp_path):
    repository = FlakyRepository(tmp_path / "agents.sqlite3", failing={core.EventType.PROGRESS_UPDATED})
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, progress(execution, 1))
    await queue.drain()
    await port.record(execution, tool_intent(execution, 2))
    await queue.aclose()

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.event_type for event in log] == [core.EventType.TOOL_INTENT]


async def test_critical_write_failure_is_raised(tmp_path):
    repository = FlakyRepository(tmp_path / "agents.sqlite3", failing={core.EventType.TOOL_INTENT})
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    with pytest.raises(OSError):
        await port.record(execution, tool_intent(execution, 1))
    await queue.aclose()


async def test_final_without_saved_candidate_fails_and_is_not_stored(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")

    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, final(execution, 1, output_refs=("out-missing",)))

    assert caught.value.code == "OUTPUT_MISSING"
    assert await port.read_execution_log(execution, execution.execution_id) == ()


async def test_final_stores_a_candidate_whose_queued_write_failed(tmp_path):
    repository = FlakyRepository(tmp_path / "agents.sqlite3", failing={core.EventType.OUTPUT_PROPOSED})
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")
    proposed = candidate(execution, 1)

    await port.record(execution, proposed)
    await queue.drain()
    repository.failing.clear()
    await port.record(execution, final(execution, 2))
    await queue.aclose()

    stored = repository.candidate_output(NS, "out-1")
    assert stored is not None and stored.event_id == proposed.event_id
    assert stored.payload.text == "进展如下"
    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.event_type for event in log] == [core.EventType.OUTPUT_PROPOSED, core.EventType.EXECUTION_FINAL]


async def test_final_stores_a_candidate_still_in_the_queue(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")

    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    assert repository.candidate_output(NS, "out-1") is not None
    await queue.aclose()


async def test_resubmitted_candidate_and_final_keep_their_ids(host):
    repository, queue, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    proposed, finished = candidate(execution, 1), final(execution, 2)

    for _ in range(2):
        await port.record(execution, proposed)
        await port.record(execution, finished)
    await queue.drain()

    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.event_id for event in log] == [proposed.event_id, finished.event_id]
    assert repository.candidate_output(NS, "out-1").event_id == proposed.event_id


async def test_output_id_cannot_be_reused_by_another_event(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, candidate(execution, 1))
    await port.record(execution, final(execution, 2))

    with pytest.raises(PersistentAgentError) as caught:
        repository.record_events([candidate(execution, 3, text="另一份内容")], now=NOW)
    assert caught.value.code == "CONFLICT"


async def test_execution_has_at_most_one_terminal_event(host):
    repository, _, port = host
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, paused(execution, 1))

    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, final(execution, 2, output_refs=()))
    assert caught.value.code == "CONFLICT"


async def test_event_of_another_execution_is_rejected(host):
    repository, _, port = host
    mine = accept_and_start(repository, "conv-1", "msg-1")
    other = accept_and_start(repository, "conv-2", "msg-2")

    with pytest.raises(BaseError):
        await port.record(mine, tool_intent(other, 1))


async def test_reading_a_log_of_another_request_is_rejected(host):
    repository, _, port = host
    mine = accept_and_start(repository, "conv-1", "msg-1")
    other = accept_and_start(repository, "conv-2", "msg-2")
    await port.record(other, tool_intent(other, 1))

    for execution_id in (other.execution_id, "exe_unknown"):
        with pytest.raises(PersistentAgentError) as caught:
            await port.read_execution_log(mine, execution_id)
        assert caught.value.code == "NOT_FOUND"


async def test_log_survives_restart(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, progress(execution, 1))
    await port.record(execution, candidate(execution, 2))
    await port.record(execution, final(execution, 3))
    await queue.aclose()

    restarted = ExecutionRepository(tmp_path / "agents.sqlite3")
    log = restarted.read_execution_log(NS, execution.session_id, execution.request_id, execution.execution_id)
    assert [event.local_seq for event in log] == [1, 2, 3]
    assert log[2].payload.output_refs == ("out-1",)


async def test_closed_queue_rejects_ordinary_events_but_writes_what_was_queued(tmp_path):
    repository = ExecutionRepository(tmp_path / "agents.sqlite3")
    queue = CommitQueue(repository)
    await queue.start()
    port = HostCommitPort(repository, queue)
    execution = accept_and_start(repository, "conv-1", "msg-1")
    await port.record(execution, progress(execution, 1))

    await queue.aclose()

    assert not queue.running
    with pytest.raises(PersistentAgentError) as caught:
        await port.record(execution, progress(execution, 2))
    assert caught.value.code == "QUEUE_CLOSED"
    log = await port.read_execution_log(execution, execution.execution_id)
    assert [event.local_seq for event in log] == [1]


class FakeBudget:
    async def admit(self, execution, model_call_id):
        raise AssertionError("not called")

    async def report(self, execution, model_call_id, usage):
        raise AssertionError("not called")


async def test_port_satisfies_the_core_contract(host):
    _, _, port = host

    core.PersistentPorts(commit=port, budget=FakeBudget(), window_backend=InMemoryKVStore()).validate()
