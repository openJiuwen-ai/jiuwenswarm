import assert from 'node:assert/strict';
import test from 'node:test';

import {
  applySubagentActivity,
  applySubagentHistoryUpdated,
  applySubagentResult,
  applySubagentTranscript,
  applySubagentTurn,
  applySubagentToolStatus,
  applySubagentUpdated,
  createEmptySubagentRuntime,
  dropCachedSubagent,
  markRunningSubagentsCancelled,
  selectSubagentActivities,
  selectSubagentHistoryRestoring,
  selectSubagentResult,
  selectSubagents,
  selectSubagentTurns,
  useSubagentStore,
} from '../node_modules/.cache/subagent-store/subagentStore.mjs';
import {
  normalizeSubagentActivityEvent,
  normalizeSubagentStatusEvent,
  normalizeSubagentToolStatusUpdates,
  normalizeSubagentWaitResults,
} from '../node_modules/.cache/subagent-store/subagentNormalizer.mjs';

const sessionId = 'session-a';

function subagent(overrides = {}) {
  return {
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    subagent_type: 'general',
    display_name: 'Agent A',
    role: 'Researcher',
    task_description: 'Research the topic',
    status: 'running',
    closed_at: null,
    closed_reason: null,
    error: null,
    created_at: 1000,
    updated_at: 1000,
    revision: 1,
    ...overrides,
  };
}

function event(revision, value = subagent({ revision })) {
  return {
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent: value,
  };
}

function activity(activityId, sequence, overrides = {}) {
  return {
    activity_id: activityId,
    subagent_id: 'agent-a',
    task_id: 'task-a',
    sequence,
    kind: 'tool_call',
    summary: activityId,
    at_ms: 1000 + sequence,
    ...overrides,
  };
}

test('subagent status updates are revision-idempotent and keep terminal history', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1));
  runtime = applySubagentUpdated(
    runtime,
    event(3, subagent({
      status: 'closed',
      closed_at: 3000,
      closed_reason: 'completed',
      updated_at: 3000,
      revision: 3,
    })),
  );

  const lateRunning = event(2, subagent({ revision: 2, updated_at: 2000 }));
  assert.equal(applySubagentUpdated(runtime, lateRunning), runtime);
  assert.equal(runtime.subagentsById['agent-a'].status, 'closed');

  const reopened = applySubagentUpdated(runtime, event(4, subagent({ revision: 4, updated_at: 4000 })));
  assert.equal(reopened, runtime);
  assert.equal(runtime.subagentsById['agent-a'].status, 'closed');
});

test('closed lifecycle requires a newer revision even when a backend resets close revision', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1, subagent({ revision: 1, updated_at: 1000 })));
  runtime = applySubagentUpdated(runtime, event(2, subagent({ status: 'idle', revision: 2, updated_at: 2000 })));
  const staleClosed = applySubagentUpdated(runtime, event(0, subagent({
    status: 'closed',
    closed_reason: 'manual',
    lifecycle: 'closed',
    can_send_input: false,
    needs_resume: true,
    revision: 0,
    updated_at: 3000,
  })));
  assert.equal(staleClosed, runtime);
  assert.equal(runtime.subagentsById['agent-a'].status, 'idle');

  runtime = applySubagentUpdated(runtime, event(3, subagent({
    status: 'closed',
    closed_reason: 'manual',
    lifecycle: 'closed',
    can_send_input: false,
    needs_resume: true,
    revision: 3,
    updated_at: 3000,
  })));
  assert.equal(runtime.subagentsById['agent-a'].status, 'closed');
  assert.equal(runtime.subagentsById['agent-a'].closed_reason, 'manual');
  assert.equal(
    applySubagentUpdated(runtime, event(3, subagent({ status: 'idle', revision: 3, updated_at: 4000 }))),
    runtime,
  );
});

test('store rejects a status event whose nested parent session differs', () => {
  const runtime = createEmptySubagentRuntime(sessionId);
  const mismatched = event(1, subagent({ parent_session_id: 'session-b' }));
  assert.equal(applySubagentUpdated(runtime, mismatched), runtime);
});

test('a lower-revision closed event cannot overwrite a terminal failure', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(5, subagent({
    status: 'closed',
    closed_reason: 'failed',
    turn_outcome: 'failed',
    updated_at: 5000,
    revision: 5,
  })));

  const lateClosed = applySubagentUpdated(runtime, event(4, subagent({
    status: 'closed',
    closed_reason: 'evicted',
    updated_at: 6000,
    revision: 4,
  })));
  assert.equal(lateClosed, runtime);
  assert.equal(runtime.subagentsById['agent-a'].closed_reason, 'failed');
});

test('same revision cannot overwrite a state even with a later timestamp', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(5, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    updated_at: 5000,
    revision: 5,
  })));

  const sameRevision = applySubagentUpdated(runtime, event(5, subagent({
    status: 'closed',
    lifecycle: 'closed',
    needs_resume: true,
    closed_reason: 'evicted',
    updated_at: 6000,
    revision: 5,
  })));
  assert.equal(sameRevision, runtime);
  assert.equal(runtime.subagentsById['agent-a'].status, 'idle');
});

test('a newer same-revision follow-up turn updates the assignment and status', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(2, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    task_description: 'Query today',
    updated_at: 2000,
    revision: 2,
  })));

  const followUp = applySubagentUpdated(runtime, event(2, subagent({
    status: 'running',
    turn_outcome: null,
    lifecycle: 'live',
    can_send_input: false,
    needs_resume: false,
    task_description: 'Query tomorrow',
    updated_at: 3000,
    revision: 2,
  })));
  assert.equal(followUp.subagentsById['agent-a'].status, 'running');
  assert.equal(followUp.subagentsById['agent-a'].task_description, 'Query tomorrow');
  assert.equal(followUp.subagentsById['agent-a'].revision, 2);
});

test('history status with the same revision but newer timestamp supersedes stale cache', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(5, subagent({
    status: 'running',
    updated_at: 5000,
    revision: 5,
  })));

  const historyIdle = event(5, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    updated_at: 6000,
    revision: 5,
  }));
  const restored = applySubagentHistoryUpdated(runtime, historyIdle);
  assert.equal(restored.subagentsById['agent-a'].status, 'idle');
  assert.equal(restored.subagentsById['agent-a'].turn_outcome, 'completed');
});

test('history status snapshots do not erase the recovered assignment', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentHistoryUpdated(runtime, event(1, subagent({
    updated_at: 1000,
    revision: 1,
  })));

  const sparseIdle = event(2, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    role: '',
    task_description: '',
    updated_at: 2000,
    revision: 2,
  }));
  const restored = applySubagentHistoryUpdated(runtime, sparseIdle);
  assert.equal(restored.subagentsById['agent-a'].status, 'idle');
  assert.equal(restored.subagentsById['agent-a'].task_description, 'Research the topic');
  assert.equal(restored.subagentsById['agent-a'].role, 'Researcher');
});

test('history status snapshots enrich a generic cached display name', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(4, subagent({
    display_name: 'general-purpose',
    role: '',
    revision: 4,
    updated_at: 4000,
  })));
  const enriched = applySubagentHistoryUpdated(runtime, event(1, subagent({
    display_name: '汕头天气查询员',
    role: '查询汕头今日天气',
    revision: 1,
    updated_at: 1000,
  })));
  assert.equal(enriched.subagentsById['agent-a'].display_name, '汕头天气查询员');
  assert.equal(enriched.subagentsById['agent-a'].role, '查询汕头今日天气');
  assert.equal(enriched.subagentsById['agent-a'].revision, 4);
});

test('history restore exposes an explicit pending state until replay finishes', () => {
  const restoreSessionId = 'history-restore-session';
  useSubagentStore.getState().removeRuntime(restoreSessionId);
  useSubagentStore.getState().ensureRuntime(restoreSessionId);
  useSubagentStore.getState().beginHistoryRestore(restoreSessionId, 'agent-a');
  assert.equal(
    selectSubagentHistoryRestoring(useSubagentStore.getState().getRuntime(restoreSessionId), 'agent-a'),
    true,
  );
  useSubagentStore.getState().finishHistoryRestore(restoreSessionId, 'agent-a');
  assert.equal(
    selectSubagentHistoryRestoring(useSubagentStore.getState().getRuntime(restoreSessionId), 'agent-a'),
    false,
  );
  useSubagentStore.getState().removeRuntime(restoreSessionId);
});


test('history can replace a stale cached closure with a newer idle revision', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(0, subagent({
    status: 'closed',
    lifecycle: 'closed',
    needs_resume: true,
    closed_reason: 'manual',
    task_description: '',
    updated_at: 7000,
    revision: 0,
  })));

  const historyIdle = event(5, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    updated_at: 6000,
    revision: 5,
  }));
  const restored = applySubagentHistoryUpdated(runtime, historyIdle);
  assert.equal(restored.subagentsById['agent-a'].status, 'idle');
  assert.equal(restored.subagentsById['agent-a'].revision, 5);
  assert.equal(restored.subagentsById['agent-a'].task_description, 'Research the topic');
});

test('successful subagent tool results recover a running roster state without changing its revision', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(4, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    updated_at: 5000,
    revision: 4,
  })));

  const updates = normalizeSubagentToolStatusUpdates({
    event_type: 'chat.tool_result',
    session_id: sessionId,
    tool_result: {
      tool_name: 'subagent_resume',
      result: "success=True data={'subagent_id': 'agent-a', 'status': 'running'} error=None",
    },
  });
  assert.deepEqual(updates, [{ subagent_id: 'agent-a', status: 'running' }]);

  const recovered = applySubagentToolStatus(runtime, updates[0].subagent_id, updates[0].status, 6000);
  assert.equal(recovered.subagentsById['agent-a'].status, 'running');
  assert.equal(recovered.subagentsById['agent-a'].revision, 4);
  assert.equal(recovered.subagentsById['agent-a'].task_description, 'Research the topic');

  const withFollowUp = applySubagentToolStatus(runtime, 'agent-a', 'running', 6000, 'Query tomorrow');
  assert.equal(withFollowUp.subagentsById['agent-a'].task_description, 'Query tomorrow');

  const completed = applySubagentUpdated(recovered, event(5, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    updated_at: 7000,
    revision: 5,
  })));
  assert.equal(completed.subagentsById['agent-a'].status, 'idle');
});

test('successful close results recover a closed roster state without changing its revision', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(3, subagent({
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    updated_at: 3000,
    revision: 3,
  })));

  const updates = normalizeSubagentToolStatusUpdates({
    event_type: 'chat.tool_result',
    session_id: sessionId,
    tool_result: {
      tool_name: 'subagent_close',
      result: "success=True data={'subagent_id': 'agent-a', 'previous_status': 'completed'} error=None",
    },
  });
  assert.deepEqual(updates, [{ subagent_id: 'agent-a', status: 'closed' }]);

  const closed = applySubagentToolStatus(runtime, updates[0].subagent_id, updates[0].status, 4000);
  assert.equal(closed.subagentsById['agent-a'].status, 'closed');
  assert.equal(closed.subagentsById['agent-a'].revision, 3);
  assert.equal(closed.subagentsById['agent-a'].lifecycle, 'closed');
  assert.equal(closed.subagentsById['agent-a'].needs_resume, true);
});

test('successful subagent spawn results restore the live assignment and turn', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1, subagent({
    task_description: '',
    updated_at: 1000,
    revision: 1,
  })));

  const updates = normalizeSubagentToolStatusUpdates({
    event_type: 'chat.tool_result',
    session_id: sessionId,
    tool_result: {
      tool_name: 'subagent_spawn',
      result: "success=True data={'subagent_id': 'agent-a', 'task_id': 'turn-a', 'status': 'running'} error=None",
    },
  });
  assert.deepEqual(updates, [{ subagent_id: 'agent-a', status: 'running', task_id: 'turn-a' }]);

  const recovered = applySubagentToolStatus(runtime, updates[0].subagent_id, updates[0].status, 1100, 'Query from spawn', updates[0].task_id);
  assert.equal(recovered.subagentsById['agent-a'].task_description, 'Query from spawn');
  assert.deepEqual(selectSubagentTurns(recovered, 'agent-a'), [{
    task_id: 'turn-a',
    task_description: 'Query from spawn',
    started_at: 1100,
  }]);
});

test('live spawn assignment fills an activity turn when the tool result has no task id', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1, subagent({
    task_description: '',
    updated_at: 1000,
    revision: 1,
  })));
  runtime = applySubagentActivity(runtime, {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('spawn-activity', 1, { task_id: 'turn-a', at_ms: 1100 }),
  });

  const recovered = applySubagentToolStatus(runtime, 'agent-a', 'running', 1200, 'Query from spawn');
  assert.equal(recovered.subagentsById['agent-a'].task_description, 'Query from spawn');
  assert.equal(selectSubagentTurns(recovered, 'agent-a')[0].task_description, 'Query from spawn');
});

test('failed subagent tool results do not change roster state', () => {
  assert.deepEqual(normalizeSubagentToolStatusUpdates({
    event_type: 'chat.tool_result',
    session_id: sessionId,
    tool_result: {
      tool_name: 'subagent_resume',
      result: "success=False data=None error={'message': 'not found'}",
    },
  }), []);
});

test('subagent_list success results recover each explicit running status', () => {
  const updates = normalizeSubagentToolStatusUpdates({
    event_type: 'chat.tool_result',
    session_id: sessionId,
    tool_result: {
      tool_name: 'subagent_list',
      result: "success=True data={'subagents': [{'subagent_id': 'agent-a', 'status': 'running'}, {'subagent_id': 'agent-b', 'status': 'idle'}]} error=None",
    },
  });
  assert.deepEqual(updates, [
    { subagent_id: 'agent-a', status: 'running' },
    { subagent_id: 'agent-b', status: 'idle' },
  ]);
});

test('a successful parent cancel settles only still-running subagents locally', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1, subagent({ updated_at: 1000 })));
  runtime = applySubagentUpdated(runtime, event(2, subagent({
    subagent_id: 'idle-agent',
    status: 'idle',
    turn_outcome: 'completed',
    updated_at: 2000,
    revision: 2,
  })));

  const cancelled = markRunningSubagentsCancelled(runtime, 3000);
  assert.equal(cancelled.subagentsById['agent-a'].status, 'idle');
  assert.equal(cancelled.subagentsById['agent-a'].turn_outcome, 'cancelled');
  assert.equal(cancelled.subagentsById['agent-a'].can_send_input, true);
  assert.equal(cancelled.subagentsById['idle-agent'].turn_outcome, 'completed');
});

test('live activities deduplicate by derived id and remain sequence ordered', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1));
  const first = {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('second', 2),
  };
  const second = {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('first', 1),
  };
  runtime = applySubagentActivity(runtime, first);
  runtime = applySubagentActivity(runtime, second);
  runtime = applySubagentActivity(runtime, second);
  assert.deepEqual(
    selectSubagentActivities(runtime, 'agent-a').map(item => item.activity_id),
    ['first', 'second'],
  );
});

test('store rejects an activity whose nested parent session differs', () => {
  const runtime = createEmptySubagentRuntime(sessionId);
  assert.equal(
    applySubagentActivity(runtime, {
      event_type: 'chat.subagent_activity',
      session_id: sessionId,
      activity: activity('cross-session', 1, { parent_session_id: 'session-b' }),
    }),
    runtime,
  );
});

test('selection prefers running subagents and orders idle before closed history', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1, subagent({ updated_at: 1000 })));
  runtime = applySubagentUpdated(runtime, event(1, subagent({
    subagent_id: 'idle-agent',
    display_name: 'Idle agent',
    status: 'idle',
    updated_at: 2000,
    revision: 1,
  })));
  runtime = applySubagentUpdated(runtime, event(1, subagent({
    subagent_id: 'closed-agent',
    display_name: 'Closed agent',
    status: 'closed',
    closed_reason: 'completed',
    updated_at: 3000,
    revision: 1,
  })));
  assert.equal(runtime.selectedSubagentId, 'agent-a');
  assert.deepEqual(selectSubagents(runtime).map(item => item.subagent_id), ['agent-a', 'idle-agent', 'closed-agent']);
});

test('normalizers accept the live Web payloads and real activity kinds', () => {
  const status = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    subagent_type: 'general',
    display_name: 'Agent A',
    role: 'Researcher',
    task_description: 'Research the topic',
    status: 'starting',
    created_at: 1000,
    updated_at: 1100,
    revision: 1,
  });
  assert.equal(status?.subagent.status, 'running');
  assert.equal(status?.subagent.created_at, 1000);

  assert.equal(normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: 'session-a',
    subagent_id: 'agent-a',
    parent_session_id: 'session-b',
    display_name: 'Agent A',
    status: 'running',
    revision: 1,
  }), null);

  const activityEvent = normalizeSubagentActivityEvent({
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    subagent_id: 'agent-a',
    task_id: 'task-a',
    seq: 7,
    kind: 'thinking',
    summary: 'checking the source',
    at_ms: 1200,
    phase_id: 3,
  });
  assert.equal(activityEvent?.activity.sequence, 7);
  assert.equal(activityEvent?.activity.kind, 'thinking');
  assert.equal(activityEvent?.activity.phase_id, 3);
  assert.equal(activityEvent?.activity.activity_id, 'agent-a:task-a:7');
  assert.equal(activityEvent?.activity.parent_session_id, sessionId);

  assert.equal(normalizeSubagentActivityEvent({
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    parent_session_id: 'session-b',
    subagent_id: 'agent-a',
    task_id: 'task-a',
    seq: 8,
    kind: 'thinking',
    at_ms: 1200,
  }), null);
});

test('live subagent runtimes stay isolated and retain activity that arrives before status', () => {
  let runtimeA = createEmptySubagentRuntime('session-a');
  const runtimeB = createEmptySubagentRuntime('session-b');

  runtimeA = applySubagentActivity(runtimeA, {
    event_type: 'chat.subagent_activity',
    session_id: 'session-a',
    activity: activity('early', 1),
  });
  assert.deepEqual(selectSubagentActivities(runtimeA, 'agent-a').map(item => item.activity_id), ['early']);
  runtimeA = applySubagentUpdated(runtimeA, event(1));
  assert.equal(
    applySubagentUpdated(runtimeB, event(1)),
    runtimeB,
  );
  assert.deepEqual(selectSubagents(runtimeB), []);
});

test('normalizers preserve live terminal reasons and reject malformed activities', () => {
  for (const [status, closedReason] of [
    ['error', 'failed'],
    ['cancelled', 'cancelled'],
    ['closed', 'evicted'],
  ]) {
    const normalized = normalizeSubagentStatusEvent({
      event_type: 'chat.subtask_update',
      session_id: sessionId,
      subagent_id: 'agent-a',
      parent_session_id: sessionId,
      display_name: 'Agent A',
      status,
      closed_reason: closedReason,
      revision: 2,
    });
    assert.equal(normalized?.subagent.status, 'closed');
    assert.equal(normalized?.subagent.closed_reason, closedReason);
  }

  assert.equal(normalizeSubagentActivityEvent({
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    subagent_id: 'agent-a',
    task_id: 'task-a',
    seq: -1,
    kind: 'thinking',
    at_ms: 1200,
  }), null);
  assert.equal(normalizeSubagentActivityEvent({
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    subagent_id: 'agent-a',
    task_id: 'task-a',
    seq: 1,
    kind: 'message',
    at_ms: 1200,
  }), null);
});

test('lifecycle closed and needs_resume take precedence over legacy status', () => {
  for (const value of [
    { status: 'idle', lifecycle: 'closed', needs_resume: false },
    { status: 'running', lifecycle: 'closed', needs_resume: false },
    { status: 'idle', lifecycle: 'live', needs_resume: true },
  ]) {
    const normalized = normalizeSubagentStatusEvent({
      event_type: 'chat.subtask_update',
      session_id: sessionId,
      parent_session_id: sessionId,
      subagent_id: 'agent-a',
      display_name: 'Agent A',
      revision: 2,
      ...value,
    });
    assert.equal(normalized?.subagent.status, 'closed');
  }
});

test('normalizers preserve idle lifecycle and map adapter projections using lifecycle fields', () => {
  const completed = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    display_name: 'Agent A',
    status: 'idle',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    closed_at: 25,
    revision: 2,
  });
  assert.equal(completed?.subagent.status, 'idle');
  assert.equal(completed?.subagent.closed_reason, null);
  assert.equal(completed?.subagent.turn_outcome, 'completed');
  assert.equal(completed?.subagent.can_send_input, true);
  assert.equal(completed?.subagent.closed_at, null);

  const adapterIdle = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    display_name: 'Agent A',
    status: 'completed',
    turn_outcome: 'completed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    revision: 3,
  });
  assert.equal(adapterIdle?.subagent.status, 'idle');
  assert.equal(adapterIdle?.subagent.closed_reason, null);

  const failed = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    display_name: 'Agent A',
    status: 'idle',
    turn_outcome: 'failed',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    message: 'turn timeout',
    revision: 3,
  });
  assert.equal(failed?.subagent.status, 'idle');
  assert.equal(failed?.subagent.closed_reason, null);
  assert.equal(failed?.subagent.error?.message, 'turn timeout');

  const cancelled = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    display_name: 'Agent A',
    status: 'completed',
    turn_outcome: 'cancelled',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    revision: 4,
  });
  assert.equal(cancelled?.subagent.status, 'idle');
  assert.equal(cancelled?.subagent.closed_reason, null);

  const closed = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-a',
    parent_session_id: sessionId,
    display_name: 'Agent A',
    status: 'closed',
    closed_reason: 'completed',
    lifecycle: 'closed',
    can_send_input: false,
    needs_resume: true,
    revision: 5,
  });
  assert.equal(closed?.subagent.status, 'closed');
  assert.equal(closed?.subagent.closed_reason, 'completed');

  const closedFailedTurn = normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-closed-failed',
    parent_session_id: sessionId,
    display_name: 'Closed failed turn',
    status: 'closed',
    lifecycle: 'closed',
    turn_outcome: 'failed',
    closed_at: 50,
    revision: 6,
  });
  assert.equal(closedFailedTurn?.subagent.closed_reason, 'failed');
  assert.equal(closedFailedTurn?.subagent.closed_at, 50);

  assert.equal(normalizeSubagentStatusEvent({
    event_type: 'chat.subtask_update',
    session_id: sessionId,
    subagent_id: 'agent-unknown-status',
    parent_session_id: sessionId,
    display_name: 'Unknown status',
    status: 'future-status',
    lifecycle: 'live',
    can_send_input: true,
    needs_resume: false,
    revision: 7,
  }), null);
});

test('subagent_wait results preserve the full body and attach to the session runtime', () => {
  const results = normalizeSubagentWaitResults({
    event_type: 'chat.tool_result',
    session_id: sessionId,
    tool_name: 'subagent_wait',
    subagent_wait: {
      statuses: { 'agent-a': 'completed' },
      results: {
        'agent-a': 'Full result\nwith formatting',
        'agent-empty': '',
        'agent-whitespace': '  \n  ',
      },
      output_files: {
        'agent-a': '/tmp/agent-a.md',
        'agent-empty': '/tmp/agent-empty.md',
      },
      timed_out: false,
    },
  });
  assert.deepEqual(results, [{
    subagent_id: 'agent-a',
    content: 'Full result\nwith formatting',
    output_file: '/tmp/agent-a.md',
  }]);

  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentResult(runtime, results[0]);
  assert.equal(selectSubagentResult(runtime, 'agent-a')?.content, 'Full result\nwith formatting');
  assert.equal(selectSubagentResult(runtime, 'agent-a')?.output_file, '/tmp/agent-a.md');
  assert.equal(applySubagentResult(runtime, results[0]), runtime);
});

test('history transcripts are deduplicated and structured wait results remain authoritative', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentTranscript(runtime, { subagent_id: 'agent-a', content: 'first part' });
  runtime = applySubagentTranscript(runtime, { subagent_id: 'agent-a', content: 'second part' });
  runtime = applySubagentTranscript(runtime, { subagent_id: 'agent-a', content: 'first part' });
  assert.equal(selectSubagentResult(runtime, 'agent-a')?.content, 'first part\n\nsecond part');

  runtime = applySubagentResult(runtime, { subagent_id: 'agent-a', content: 'final wait result' });
  runtime = applySubagentTranscript(runtime, { subagent_id: 'agent-a', content: 'late transcript' });
  assert.equal(selectSubagentResult(runtime, 'agent-a')?.content, 'final wait result');
});

test('follow-up turns keep queries, activities, and final results separate', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-1', 'Query today', 1000);
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-2', 'Query tomorrow', 2000);
  runtime = applySubagentActivity(runtime, {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('turn-1-activity', 1, { task_id: 'turn-1', at_ms: 1100 }),
  });
  runtime = applySubagentActivity(runtime, {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('turn-2-activity', 2, { task_id: 'turn-2', at_ms: 2100 }),
  });
  runtime = applySubagentTranscript(runtime, {
    subagent_id: 'agent-a',
    task_id: 'turn-1',
    at_ms: 1200,
    content: 'Today result',
  });
  runtime = applySubagentTranscript(runtime, {
    subagent_id: 'agent-a',
    task_id: 'turn-2',
    at_ms: 2200,
    content: 'Tomorrow result',
  });

  const turns = selectSubagentTurns(runtime, 'agent-a');
  assert.deepEqual(turns.map(turn => ({
    task_id: turn.task_id,
    task_description: turn.task_description,
    activities: selectSubagentActivities(runtime, 'agent-a').filter(item => item.task_id === turn.task_id).length,
    result: turn.result?.content,
  })), [
    { task_id: 'turn-1', task_description: 'Query today', activities: 1, result: 'Today result' },
    { task_id: 'turn-2', task_description: 'Query tomorrow', activities: 1, result: 'Tomorrow result' },
  ]);
});

test('historical activity does not overwrite an existing turn assignment', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(3, subagent({
    task_description: 'Query tomorrow',
    updated_at: 3000,
    revision: 3,
  })));
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-1', 'Query today', 1000);
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-2', 'Query tomorrow', 2000);
  runtime = applySubagentActivity(runtime, {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('turn-1-activity', 1, { task_id: 'turn-1', at_ms: 1100 }),
  });

  assert.deepEqual(selectSubagentTurns(runtime, 'agent-a').map(turn => ({
    task_id: turn.task_id,
    task_description: turn.task_description,
  })), [
    { task_id: 'turn-1', task_description: 'Query today' },
    { task_id: 'turn-2', task_description: 'Query tomorrow' },
  ]);
});

test('explicit turn assignment repairs a fallback created by early activity', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentUpdated(runtime, event(1, subagent({
    task_description: 'Query first',
    updated_at: 1000,
    revision: 1,
  })));
  runtime = applySubagentActivity(runtime, {
    event_type: 'chat.subagent_activity',
    session_id: sessionId,
    activity: activity('turn-2-activity', 2, { task_id: 'turn-2', at_ms: 2100 }),
  });
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-2', 'Query second', 2000);

  assert.deepEqual(selectSubagentTurns(runtime, 'agent-a').map(turn => ({
    task_id: turn.task_id,
    task_description: turn.task_description,
  })), [
    { task_id: 'turn-2', task_description: 'Query second' },
  ]);
});

test('a wait result received before turn metadata is attached after turns are restored', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentResult(runtime, {
    subagent_id: 'agent-a',
    content: 'Restored final',
    source: 'wait',
  });
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-1', 'Query today', 1000);
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-2', 'Query tomorrow', 2000);
  assert.equal(selectSubagentTurns(runtime, 'agent-a')[0].result?.content, 'Restored final');
});

test('transcript finals survive a wait result and attach to later restored turns', () => {
  let runtime = createEmptySubagentRuntime(sessionId);
  runtime = applySubagentResult(runtime, {
    subagent_id: 'agent-a',
    content: 'Latest wait result',
    source: 'wait',
  });
  runtime = applySubagentTranscript(runtime, { subagent_id: 'agent-a', content: 'First final', at_ms: 1100 });
  runtime = applySubagentTranscript(runtime, { subagent_id: 'agent-a', content: 'Second final', at_ms: 2100 });
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-1', 'Query one', 1000);
  runtime = applySubagentTurn(runtime, 'agent-a', 'turn-2', 'Query two', 2000);
  const turns = selectSubagentTurns(runtime, 'agent-a');
  assert.equal(turns[0].result?.content, 'Latest wait result');
  assert.equal(turns[1].result?.content, 'Second final');
});

test('actual live runtime snapshots rehydrate after a page refresh', () => {
  const previousWindow = globalThis.window;
  const values = new Map();
  globalThis.window = {
    sessionStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
  };
  try {
    useSubagentStore.getState().removeRuntime(sessionId);
    useSubagentStore.getState().applyEvent(sessionId, event(1));
    useSubagentStore.getState().applyEvent(sessionId, {
      event_type: 'chat.subagent_activity',
      session_id: sessionId,
      activity: activity('persisted', 1),
    });
    const restored = createEmptySubagentRuntime(sessionId);
    assert.equal(restored.subagentsById['agent-a'].status, 'running');
    assert.deepEqual(selectSubagentActivities(restored, 'agent-a').map(item => item.activity_id), ['persisted']);
  } finally {
    useSubagentStore.getState().removeRuntime(sessionId);
    globalThis.window = previousWindow;
  }
});

test('session cache rejects a subagent and activity from another parent session', () => {
  const previousWindow = globalThis.window;
  const values = new Map([
    [`jiuwen.subagent.runtime.v1:${encodeURIComponent(sessionId)}`, JSON.stringify({
      subagents: [subagent({ parent_session_id: 'session-b' })],
      activities: [activity('cross-session', 1, { parent_session_id: 'session-b' })],
      results: [],
      selectedSubagentId: 'agent-a',
    })],
  ]);
  globalThis.window = {
    sessionStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
  };
  try {
    const restored = createEmptySubagentRuntime(sessionId);
    assert.deepEqual(selectSubagents(restored), []);
    assert.deepEqual(selectSubagentActivities(restored, 'agent-a'), []);
  } finally {
    globalThis.window = previousWindow;
  }
});

test('session cache does not replace a state with the same revision', () => {
  const previousWindow = globalThis.window;
  const storageKey = `jiuwen.subagent.runtime.v1:${encodeURIComponent(sessionId)}`;
  const values = new Map();
  globalThis.window = {
    sessionStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
  };
  try {
    useSubagentStore.getState().removeRuntime(sessionId);
    useSubagentStore.getState().applyEvent(sessionId, event(5, subagent({
      status: 'idle',
      lifecycle: 'live',
      can_send_input: true,
      needs_resume: false,
      turn_outcome: 'completed',
      revision: 5,
      updated_at: 5000,
    })));
    values.set(storageKey, JSON.stringify({
      subagents: [subagent({
        status: 'closed',
        lifecycle: 'closed',
        needs_resume: true,
        closed_reason: 'evicted',
        revision: 5,
        updated_at: 6000,
      })],
      activities: [],
      results: [],
      selectedSubagentId: 'agent-a',
    }));
    useSubagentStore.getState().hydrateRuntime(sessionId);
    const restored = useSubagentStore.getState().getRuntime(sessionId);
    assert.equal(restored?.subagentsById['agent-a'].status, 'idle');
    assert.equal(restored?.subagentsById['agent-a'].turn_outcome, 'completed');
  } finally {
    globalThis.window = previousWindow;
  }
});

test('session cache does not reopen a closed state with a newer revision', () => {
  const previousWindow = globalThis.window;
  const storageKey = `jiuwen.subagent.runtime.v1:${encodeURIComponent(sessionId)}`;
  const values = new Map();
  globalThis.window = {
    sessionStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
  };
  try {
    useSubagentStore.getState().removeRuntime(sessionId);
    useSubagentStore.getState().applyEvent(sessionId, event(5, subagent({
      status: 'closed',
      lifecycle: 'closed',
      needs_resume: true,
      can_send_input: false,
      closed_reason: 'completed',
      revision: 5,
      updated_at: 5000,
    })));
    values.set(storageKey, JSON.stringify({
      subagents: [subagent({
        status: 'running',
        lifecycle: 'live',
        needs_resume: false,
        can_send_input: true,
        closed_reason: null,
        revision: 6,
        updated_at: 6000,
      })],
      activities: [],
      results: [],
      selectedSubagentId: 'agent-a',
    }));
    useSubagentStore.getState().hydrateRuntime(sessionId);
    const restored = useSubagentStore.getState().getRuntime(sessionId);
    assert.equal(restored?.subagentsById['agent-a'].status, 'closed');
    assert.equal(restored?.subagentsById['agent-a'].revision, 5);
  } finally {
    useSubagentStore.getState().removeRuntime(sessionId);
    globalThis.window = previousWindow;
  }
});

test('empty history removes storage-only state but keeps a live update', () => {
  const previousWindow = globalThis.window;
  const values = new Map();
  globalThis.window = {
    sessionStorage: {
      getItem: key => values.get(key) ?? null,
      setItem: (key, value) => values.set(key, value),
      removeItem: key => values.delete(key),
    },
  };
  try {
    useSubagentStore.getState().removeRuntime(sessionId);
    useSubagentStore.getState().applyEvent(sessionId, event(1));
    const cached = createEmptySubagentRuntime(sessionId);
    const cleared = dropCachedSubagent(cached, 'agent-a', 1, 1000);
    assert.equal(cleared.subagentsById['agent-a'], undefined);

    const live = applySubagentUpdated(cached, event(2, subagent({ revision: 2, updated_at: 2000 })));
    assert.equal(dropCachedSubagent(live, 'agent-a', 1, 1000), live);
  } finally {
    useSubagentStore.getState().removeRuntime(sessionId);
    globalThis.window = previousWindow;
  }
});
