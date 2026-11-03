import assert from 'node:assert/strict';
import test from 'node:test';

import { parseTeamHistoryPanelRecords } from '../node_modules/.cache/team-history-panel/teamHistoryPanelRestore.mjs';

function spawnRecord() {
  return {
    mode: 'team',
    event_type: 'chat.tool_call',
    timestamp: 1_700_000_000_000,
    tool_call: { name: 'spawn_member', arguments: JSON.stringify({ member_name: 'dev-1' }) },
  };
}

function shutdownResultRecord(fields) {
  return {
    mode: 'team',
    event_type: 'chat.tool_result',
    timestamp: 1_700_000_001_000,
    tool_name: 'shutdown_member',
    tool_call_id: 'call-1',
    ...fields,
  };
}

test('member shutdown is read from rendered_result, the text the model read', () => {
  const state = parseTeamHistoryPanelRecords(
    [
      spawnRecord(),
      shutdownResultRecord({
        result: "success=True data={'member_name': 'dev-1'} error=None",
        rendered_result: 'Member shutdown: member_name=dev-1',
      }),
    ],
    'session-1',
  );

  assert.deepEqual(state.members.map((member) => member.member_id), []);
});

test('records without rendered_result still parse the compatibility result', () => {
  const state = parseTeamHistoryPanelRecords(
    [spawnRecord(), shutdownResultRecord({ result: 'Member shutdown: member_name=dev-1' })],
    'session-1',
  );

  assert.deepEqual(state.members.map((member) => member.member_id), []);
});

let memberEventSeq = 0;

function memberEventRecord(event) {
  memberEventSeq += 1;
  return {
    role: 'assistant',
    event_type: 'team.member',
    mode: 'team',
    session_id: 'session-1',
    timestamp: 1_700_000_100 + memberEventSeq,
    event: { team_id: 'team-1', ...event },
  };
}

function retireRecords(memberId, shutdownFields, settledStatus) {
  return [
    memberEventRecord({ type: 'team.member.spawned', member_id: memberId, mode: 'teammate', role: 'teammate', name: `Writer ${memberId}` }),
    memberEventRecord({ type: 'team.member.status_changed', member_id: memberId, old_status: 'starting', new_status: 'ready' }),
    memberEventRecord({ type: 'team.member.shutdown', member_id: memberId, force: false, ...shutdownFields }),
    memberEventRecord({ type: 'team.member.status_changed', member_id: memberId, old_status: 'shutdown_requested', new_status: settledStatus }),
  ];
}

test('a dormant retirement keeps the member on the roster as dormant', () => {
  const state = parseTeamHistoryPanelRecords(
    retireRecords('r2-writer-1', { target_status: 'dormant' }, 'dormant'),
    'session-1',
  );

  assert.equal(state.members.length, 1);
  assert.equal(state.members[0].member_id, 'r2-writer-1');
  assert.equal(state.members[0].status, 'dormant');
  assert.equal(state.members[0].name, 'Writer r2-writer-1');
});

test('legacy history without target_status restores a member that settled dormant', () => {
  const state = parseTeamHistoryPanelRecords(retireRecords('r2-writer-2', {}, 'dormant'), 'session-1');

  assert.equal(state.members.length, 1);
  assert.equal(state.members[0].status, 'dormant');
  assert.equal(state.members[0].name, 'Writer r2-writer-2');
  assert.equal(state.members[0].role, 'teammate');
});

test('a shut_down retirement removes the member', () => {
  for (const shutdownFields of [{ target_status: 'shut_down' }, {}]) {
    const state = parseTeamHistoryPanelRecords(retireRecords('temp-1', shutdownFields, 'shut_down'), 'session-1');

    assert.deepEqual(state.members.map((member) => member.member_id), []);
    // 离开名册但保留身份，任务卡片还要按 member_id 查名字。
    assert.deepEqual(
      state.departedMembers.map((member) => [member.member_id, member.name, member.status]),
      [['temp-1', 'Writer temp-1', 'shut_down']],
    );
  }
});

test('a dormant retirement is not listed as departed', () => {
  const state = parseTeamHistoryPanelRecords(
    retireRecords('r2-writer-4', { target_status: 'dormant' }, 'dormant'),
    'session-1',
  );

  assert.deepEqual(state.departedMembers, []);
});

test('a woken dormant member returns to ready', () => {
  const state = parseTeamHistoryPanelRecords(
    [
      ...retireRecords('r2-writer-3', { target_status: 'dormant' }, 'dormant'),
      memberEventRecord({ type: 'team.member.status_changed', member_id: 'r2-writer-3', old_status: 'dormant', new_status: 'restarting' }),
      memberEventRecord({ type: 'team.member.status_changed', member_id: 'r2-writer-3', old_status: 'restarting', new_status: 'ready' }),
    ],
    'session-1',
  );

  assert.equal(state.members.length, 1);
  assert.equal(state.members[0].status, 'ready');
});

test('task flow events are restored as an ordered timeline for the group chat', () => {
  const taskRecord = (type, taskId, timestamp, extra = {}) => ({
    mode: 'team',
    event_type: 'team.task',
    session_id: 'session-1',
    timestamp,
    event: { type, task_id: taskId, ...extra },
  });
  const state = parseTeamHistoryPanelRecords(
    [
      taskRecord('team.task.created', 'bless-1', 1_792_080_714, { status: 'pending', title: '写下第 1 句祝福语' }),
      taskRecord('team.task.started', 'bless-1', 1_792_080_715, { status: 'in_progress', member_id: 'writer' }),
      taskRecord('team.task.completed', 'bless-1', 1_792_080_718, { status: 'completed', member_id: 'writer' }),
    ],
    'session-1',
  );

  assert.deepEqual(
    state.taskTimeline.map((item) => [item.type, item.task_id, item.member_id]),
    [
      ['team.task.created', 'bless-1', undefined],
      ['team.task.started', 'bless-1', 'writer'],
      ['team.task.completed', 'bless-1', 'writer'],
    ],
  );
});
