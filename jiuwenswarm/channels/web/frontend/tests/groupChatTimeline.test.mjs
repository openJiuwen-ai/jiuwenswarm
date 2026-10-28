import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildGroupChatItems,
  collectTimelineTitles,
  inferLegacyHandoff,
  mergeTaskTimeline,
  parseTeamHandoff,
  toTaskTimelineEntry,
} from '../node_modules/.cache/group-chat-timeline/groupChatTimeline.mjs';

function entry(type, taskId, timestamp, extra = {}) {
  return toTaskTimelineEntry({ type, task_id: taskId, ...extra }, timestamp);
}

test('only flow-relevant task events enter the timeline', () => {
  assert.equal(entry('team.task.unblocked', 'bless-2', 1), null);
  assert.equal(entry('team.task.completed', '', 1), null);
  assert.deepEqual(entry('team.task.completed', 'bless-1', 5, { member_id: 'writer' }), {
    id: 'task-timeline-team.task.completed-bless-1-5',
    type: 'team.task.completed',
    task_id: 'bless-1',
    timestamp: 5,
    member_id: 'writer',
    title: undefined,
  });
});

test('merge drops live duplicates of restored events and keeps later repeats', () => {
  const restored = [entry('team.task.revision_requested', 't-1', 1_000)];
  const live = [
    entry('team.task.revision_requested', 't-1', 4_000),
    entry('team.task.revision_requested', 't-1', 120_000),
  ];

  const merged = mergeTaskTimeline(restored, live);

  assert.deepEqual(merged.map((item) => item.timestamp), [1_000, 120_000]);
});

test('consecutive created events collapse into one row and interleave with messages', () => {
  const timeline = [
    entry('team.task.created', 'bless-1', 100, { title: 'a' }),
    entry('team.task.created', 'bless-2', 100, { title: 'b' }),
    entry('team.task.completed', 'bless-1', 300, { member_id: 'writer' }),
  ];
  const messages = [
    { message: 'handoff-1', timestamp: 200 },
    { message: 'handoff-2', timestamp: 400 },
  ];

  const items = buildGroupChatItems(messages, timeline);

  assert.deepEqual(
    items.map((item) => (item.kind === 'message' ? item.message : `${item.type}x${item.entries.length}`)),
    ['team.task.createdx2', 'handoff-1', 'team.task.completedx1', 'handoff-2'],
  );
});

test('task events sort before a message at the same instant', () => {
  const items = buildGroupChatItems(
    [{ message: 'handoff', timestamp: 100 }],
    [entry('team.task.claimed', 't-1', 100)],
  );

  assert.deepEqual(items.map((item) => item.kind), ['task', 'message']);
});

test('completed right after verified is folded into verified', () => {
  const items = buildGroupChatItems(
    [],
    [entry('team.task.verified', 't-1', 1_000), entry('team.task.completed', 't-1', 1_200)],
  );

  assert.deepEqual(items.map((item) => item.type), ['team.task.verified']);
});

test('handoff parsing keeps known fields and rejects shapeless values', () => {
  assert.equal(parseTeamHandoff(undefined), undefined);
  assert.equal(parseTeamHandoff({ template: 'x' }), undefined);
  assert.deepEqual(parseTeamHandoff({ kind: 'task_start', task_id: 'bless-1', task_title: '写祝福', extra: 1 }), {
    kind: 'task_start',
    template: undefined,
    task_id: 'bless-1',
    task_title: '写祝福',
    max_rounds: undefined,
  });
});

test('started events are kept for inference but never rendered as rows', () => {
  const items = buildGroupChatItems([], [entry('team.task.started', 't-1', 1, { member_id: 'writer' })]);

  assert.deepEqual(items, []);
});

test('legacy empty handoff is inferred from the nearest started event of the same member', () => {
  const timeline = [
    entry('team.task.started', 'bless-1', 1_000, { member_id: 'writer' }),
    entry('team.task.started', 'bless-2', 9_000, { member_id: 'writer' }),
    entry('team.task.started', 'other', 1_005, { member_id: 'someone-else' }),
  ];

  assert.deepEqual(inferLegacyHandoff('writer', 1_010, timeline), { kind: 'task_start', task_id: 'bless-1' });
  assert.equal(inferLegacyHandoff('writer', 5_000, timeline), undefined);
  assert.equal(inferLegacyHandoff(undefined, 1_010, timeline), undefined);
});

test('titles come from whichever timeline events carry them', () => {
  const titles = collectTimelineTitles([
    entry('team.task.created', 'bless-1', 1, { title: '写下第 1 句祝福语' }),
    entry('team.task.completed', 'bless-1', 2),
  ]);

  assert.equal(titles.get('bless-1'), '写下第 1 句祝福语');
});
