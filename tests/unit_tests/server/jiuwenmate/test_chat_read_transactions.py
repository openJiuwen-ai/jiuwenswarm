"""History and list reads must not reserve SQLite's single writer lock."""

import sqlite3
from datetime import datetime, timezone

import pytest

from jiuwenswarm.server.jiuwenmate.execution_control_service import ControlStore
from jiuwenswarm.server.jiuwenmate.execution_models import AccessSnapshot, ConversationRef
from jiuwenswarm.server.jiuwenmate.execution_repository import ExecutionRepository
from jiuwenswarm.server.jiuwenmate.recovery_service import RecoveryStore
from jiuwenswarm.server.jiuwenmate.spec_provider import publish_spec
from jiuwenswarm.server.jiuwenmate.web_conversations import WebConversationStore
from openjiuwen.harness import persistent_agent as core

NOW = datetime(2026, 10, 31, tzinfo=timezone.utc)


@pytest.mark.parametrize('query', [
    'find_session', 'get_session', 'get_request', 'session_requests', 'latest_session_request',
    'executions', 'read_execution_log', 'candidate_output', 'result_deliveries',
    'pending_continuation_ref', 'control', 'recovery', 'require_open', 'known', 'list',
])
def test_chat_selects_succeed_while_another_connection_reserves_the_writer(tmp_path, monkeypatch, query):
    path = tmp_path / 'agents.sqlite3'
    sessions = ExecutionRepository(path)
    controls = ControlStore(path)
    recoveries = RecoveryStore(path)
    conversations = WebConversationStore(path)
    conversation = ConversationRef.web('conversation')
    session = sessions.resolve_session('local', 'employee', conversation, now=NOW)
    snapshot = publish_spec('employee', 'v1', {'card': {'id': 'employee', 'name': 'Test employee'}})
    request, _ = sessions.accept_request(
        session, snapshot=snapshot, source_ref='web:message',
        access=AccessSnapshot(principal_ref='user', startup_scope=frozenset(), source_scope=frozenset(), policy_revision='v1'),
        request_input=core.RequestInput(kind='scheduled', text='Test message', reply_target='conversation'),
        received_at=NOW,
    )
    request_id = request.record.request_id
    execution = sessions.start_execution('local', request_id, now=NOW)
    conversations.touch('local', 'employee', 'conversation', text='Test message', now=NOW)
    reads = {
        'find_session': (lambda: sessions.find_session('local', 'employee', conversation), session),
        'get_session': (lambda: sessions.get_session('local', session.session_id), session),
        'get_request': (lambda: sessions.get_request('local', request_id).record.request_id, request_id),
        'session_requests': (lambda: [r.record.request_id for r in sessions.session_requests('local', session.session_id)], [request_id]),
        'latest_session_request': (lambda: sessions.latest_session_request('local', session.session_id).record.request_id, request_id),
        'executions': (lambda: [e.execution_id for e in sessions.executions('local', request_id)], [execution.execution_id]),
        'read_execution_log': (lambda: sessions.read_execution_log('local', session.session_id, request_id, execution.execution_id), ()),
        'candidate_output': (lambda: sessions.candidate_output('local', 'missing-output'), None),
        'result_deliveries': (lambda: sessions.result_deliveries('local', request_id), []),
        'pending_continuation_ref': (lambda: sessions.pending_continuation_ref('local', request_id), None),
        'control': (lambda: controls.get('local', request_id), None),
        'recovery': (lambda: recoveries.latest('local', request_id), None),
        'require_open': (lambda: conversations.require_open('local', 'employee', 'conversation'), None),
        'known': (lambda: conversations.known('local', 'employee', 'conversation'), True),
        'list': (lambda: [c.conversation_id for c in conversations.list('local', 'employee')], ['conversation']),
    }
    def connect():
        conn = sqlite3.connect(path, timeout=0.05, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn
    for store in (sessions, controls, recoveries, conversations):
        monkeypatch.setattr(store, '_connect', connect)
    writer = sqlite3.connect(path, isolation_level=None)
    try:
        writer.execute('BEGIN IMMEDIATE')
        read, expected = reads[query]
        assert read() == expected
    finally:
        writer.rollback()
        writer.close()


def test_explicit_read_transaction_rejects_mutations_and_default_write_rolls_back(tmp_path):
    store = WebConversationStore(tmp_path / 'agents.sqlite3')
    store.touch('local', 'employee', 'conversation', text='Original title', now=NOW)
    with pytest.raises(sqlite3.OperationalError, match='readonly'):
        with store._transaction(write=False) as conn:
            conn.execute("UPDATE web_conversations SET title='Wrong title'")
    assert store.list('local', 'employee')[0].title == 'Original title'
    with pytest.raises(RuntimeError):
        with store._transaction() as conn:
            conn.execute("UPDATE web_conversations SET title='Uncommitted title'")
            raise RuntimeError('abort write')
    assert store.list('local', 'employee')[0].title == 'Original title'


@pytest.mark.asyncio
async def test_history_worker_queue_work_is_bounded_as_message_count_grows(monkeypatch):
    import asyncio
    import threading
    from types import SimpleNamespace
    from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager

    owner_thread = threading.get_ident()
    class Scheduler:
        @property
        def running(self):
            assert threading.get_ident() == owner_thread, 'Scheduler state belongs to the event loop'
            return ()
    states = []
    manager = object.__new__(AgentManager)
    manager._scheduler = Scheduler()
    manager._sessions = SimpleNamespace(
        find_session=lambda *args: SimpleNamespace(session_id='session'),
        session_requests=lambda *args: list(states),
        result_deliveries=lambda *args: [],
    )
    manager._result = lambda state: ((), (), None, None)
    manager._control = SimpleNamespace(control_of=lambda *args: None)
    manager._recovery = SimpleNamespace(recovery_of=lambda *args: None)
    manager._permission_approvals = SimpleNamespace(pending_for_request=lambda *args: None)
    jobs = []
    original = asyncio.to_thread
    async def recorded(function, *args, **kwargs):
        jobs.append(function)
        return await original(function, *args, **kwargs)
    monkeypatch.setattr(asyncio, 'to_thread', recorded)
    budgets = []
    for count in (1, 25):
        states[:] = [SimpleNamespace(record=SimpleNamespace(namespace_id='local', request_id=f'request-{i}')) for i in range(count)]
        jobs.clear()
        views = await manager.conversation_requests('local', 'employee', ConversationRef.web('conversation'))
        assert [v.state.record.request_id for v in views] == [f'request-{i}' for i in range(count)]
        budgets.append(len(jobs))
    assert budgets[1] <= budgets[0], 'A longer history must not enqueue one worker job per message field'


@pytest.mark.asyncio
async def test_history_observes_a_request_that_starts_while_states_are_read():
    import asyncio
    import threading
    from types import SimpleNamespace
    from jiuwenswarm.server.jiuwenmate.agent_manager import AgentManager

    loop = asyncio.get_running_loop()
    owner_thread = threading.get_ident()
    active = []
    record = SimpleNamespace(namespace_id='local', request_id='request')
    state = SimpleNamespace(record=record, status='dispatched')
    class Scheduler:
        @property
        def running(self):
            assert threading.get_ident() == owner_thread
            return tuple(active)
    async def started():
        active.append(SimpleNamespace(record=record))
    def read_states(*args):
        asyncio.run_coroutine_threadsafe(started(), loop).result(timeout=5)
        return [state]
    manager = object.__new__(AgentManager)
    manager._scheduler = Scheduler()
    manager._sessions = SimpleNamespace(
        find_session=lambda *args: SimpleNamespace(session_id='session'),
        session_requests=read_states, result_deliveries=lambda *args: [],
    )
    manager._result = lambda state: ((), (), None, None)
    manager._control = SimpleNamespace(control_of=lambda *args: None)
    manager._recovery = SimpleNamespace(recovery_of=lambda *args: None)
    manager._permission_approvals = SimpleNamespace(pending_for_request=lambda *args: None)
    views = await manager.conversation_requests('local', 'employee', ConversationRef.web('conversation'))
    assert views[0].state.status == 'dispatched'
    assert views[0].running is True


def test_history_read_batch_reuses_connections_across_repositories_and_releases_each_transaction(tmp_path, monkeypatch):
    path = tmp_path / 'agents.sqlite3'
    sessions = ExecutionRepository(path)
    controls = ControlStore(path)
    opened = []
    original = sessions._connect
    def connect():
        connection = original()
        opened.append(connection)
        return connection
    monkeypatch.setattr(sessions, '_connect', connect)
    monkeypatch.setattr(controls, '_connect', connect)
    with sessions.read_batch():
        assert sessions.find_session('local', 'employee', ConversationRef.web('new')) is None
        assert controls.get('local', 'missing') is None
        assert len(opened) == 1
        assert not opened[0].in_transaction
        # A completed SELECT must not leave a snapshot blocking a concurrent commit.
        with sqlite3.connect(path, timeout=0.05) as writer:
            writer.execute("INSERT INTO sessions VALUES ('local','session','employee','web_direct','web','new',1,'2026-10-31T00:00:00+00:00')")
        assert sessions.find_session('local', 'employee', ConversationRef.web('new')).session_id == 'session'
        assert len(opened) == 1
    with pytest.raises(sqlite3.ProgrammingError, match='closed'):
        opened[0].execute('SELECT 1')


def test_nested_read_batches_are_read_only_and_cleanup_after_failure(tmp_path):
    store = WebConversationStore(tmp_path / 'agents.sqlite3')
    with pytest.raises(RuntimeError, match='abort batch'):
        with store.read_batch():
            with store.read_batch():
                with store._transaction(write=False) as outer:
                    with store._transaction(write=False) as inner:
                        assert inner is outer
                        with pytest.raises(sqlite3.OperationalError, match='readonly'):
                            inner.execute("INSERT INTO web_conversations (namespace_id) VALUES ('forbidden')")
            raise RuntimeError('abort batch')
    store.touch('local', 'employee', 'new', text='Allowed write', now=NOW)
    assert store.list('local', 'employee')[0].title == 'Allowed write'


def test_read_batch_connections_are_not_shared_across_threads(tmp_path, monkeypatch):
    import threading
    from concurrent.futures import ThreadPoolExecutor
    store = WebConversationStore(tmp_path / 'agents.sqlite3')
    barrier = threading.Barrier(2)
    observed = []
    original = store._connect
    def connect():
        conn = original()
        observed.append((threading.get_ident(), conn))
        return conn
    monkeypatch.setattr(store, '_connect', connect)
    def read():
        with store.read_batch():
            barrier.wait(timeout=5)
            return store.list('local', 'employee')
    with ThreadPoolExecutor(max_workers=2) as pool:
        futures = [pool.submit(read), pool.submit(read)]
        assert [f.result(timeout=5) for f in futures] == [[], []]
    assert len(observed) == 2 and observed[0][0] != observed[1][0]
    assert observed[0][1] is not observed[1][1]
