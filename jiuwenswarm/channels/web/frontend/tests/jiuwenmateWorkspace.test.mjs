import assert from 'node:assert/strict';
import test from 'node:test';

import {
  closeChannelTrace,
  createWorkspaceState,
  mergeWorkspaceConversations,
  openChannelTrace,
  selectChannel,
  selectColleague,
  selectManagementChannel,
  selectRecordTab,
  selectTraceRow,
  setRecordsCollapsed,
  setTraceMetric,
  toggleTraceFilter,
  toggleTraceTurn,
  traceTurnViews,
} from '../node_modules/.cache/jiuwenmate-workspace/workspaceState.js';

const rows = [
  { id: 'system', turn: 0, role: 'system', content: 'You are JiuwenSwarm' },
  { id: 't1-context', turn: 1, role: 'context', content: '<system-reminder>' },
  { id: 't1-user', turn: 1, role: 'user', content: '你收到一条消息' },
  { id: 't1-assistant', turn: 1, role: 'assistant', content: '我是JiuwenSwarm' },
  { id: 't3-user', turn: 3, role: 'user', content: '天气' },
  { id: 't3-plan', turn: 3, role: 'assistant', content: '我来搜索' },
  { id: 't3-tool', turn: 3, role: 'tool', content: 'paid_search（"query"）' },
  { id: 't3-answer', turn: 3, role: 'assistant', content: '杭州天气' },
];

test('keeps the selected colleague conversations visible while its list refreshes', () => {
  const selected = {
    id: 'conversation-learning',
    title: '你能做什么',
    updatedAt: '2026-10-22T00:00:00Z',
    agentId: 'agent-learning',
  };
  const other = {
    id: 'conversation-other',
    title: '其他会话',
    updatedAt: '2026-10-21T00:00:00Z',
    agentId: 'agent-other',
  };

  const duringRefresh = mergeWorkspaceConversations([selected, other], 'agent-learning', [], false);
  assert.ok(duringRefresh.some((conversation) => conversation.id === selected.id));

  const afterRefresh = mergeWorkspaceConversations([selected, other], 'agent-learning', [selected], true);
  assert.ok(afterRefresh.some((conversation) => conversation.id === selected.id));
  assert.ok(afterRefresh.some((conversation) => conversation.id === other.id));
});

test('pinned conversations stay above unpinned conversations across colleagues', () => {
  const pinned = { id: 'older', agentId: 'agent-1', title: 'Pinned', updatedAt: '1', pinned: true };
  const recent = { id: 'newer', agentId: 'agent-2', title: 'Recent', updatedAt: '9', pinned: false };
  assert.deepEqual(mergeWorkspaceConversations([recent, pinned], 'agent-2', [recent], true).map((item) => item.id), [
    'older',
    'newer',
  ]);
});

test('workspace selections switch across colleagues, tabs and channels', () => {
  const initial = createWorkspaceState('after-sales-assistant', 'robot-trial-group');
  assert.equal(selectColleague(initial, 'system-architect').selectedColleagueId, 'system-architect');

  const channels = selectRecordTab(initial, 'channels');
  assert.equal(channels.recordTab, 'channels');
  const switched = selectChannel(channels, 'new-product-feedback-group');
  assert.equal(switched.selectedChannelId, 'new-product-feedback-group');
  assert.equal(switched.traceOpen, false);

  assert.equal(setRecordsCollapsed(initial, true).recordsCollapsed, true);
});

test('opening a management channel keeps the conversation list selected', () => {
  const initial = createWorkspaceState('agent-learning', null);
  const selected = selectManagementChannel(initial, 'group-1');

  assert.equal(selected.recordTab, 'conversations');
  assert.equal(selected.managementChannelId, 'group-1');
});

test('trace filters are mutually exclusive toggles and reset on close', () => {
  const opened = openChannelTrace(createWorkspaceState('after-sales-assistant', 'robot-trial-group'));
  assert.equal(opened.traceOpen, true);
  assert.equal(opened.recordTab, 'channels');
  assert.equal(opened.traceFilter, 'none');
  assert.equal(setTraceMetric(opened, 'tokens').traceMetric, 'tokens');

  const turnOnly = toggleTraceFilter(opened, 'turn');
  assert.equal(turnOnly.traceFilter, 'turn');
  assert.equal(toggleTraceFilter(turnOnly, 'tool').traceFilter, 'tool');
  assert.equal(toggleTraceFilter(turnOnly, 'turn').traceFilter, 'none');

  const selected = selectTraceRow(opened, 't1-user');
  assert.equal(selected.selectedTraceRowId, 't1-user');
  assert.equal(selectTraceRow(selected, 't1-user').selectedTraceRowId, null);

  const closed = closeChannelTrace(selected);
  assert.equal(closed.traceOpen, false);
  assert.equal(closed.selectedTraceRowId, null);
});

test('default trace view collapses tool calls inside each turn', () => {
  const views = traceTurnViews(rows, 'none', []);
  assert.deepEqual(views.map((view) => view.turn), [1, 3]);
  const turn3 = views[1].items;
  assert.deepEqual(turn3.map((item) => item.kind), ['row', 'row', 'collapsed', 'row']);
  assert.equal(turn3[2].onlyTools, true);
  assert.deepEqual(turn3[2].toolNames, ['paid search']);

  assert.ok(traceTurnViews(rows, 'tool', [])[1].items.every((item) => item.kind === 'row'));
  assert.ok(traceTurnViews(rows, 'none', [3])[1].items.every((item) => item.kind === 'row'));
});

test('turn-only view keeps the first row and summarizes steps and tools', () => {
  const views = traceTurnViews(rows, 'turn', []);
  assert.deepEqual(views[0].items.map((item) => item.kind), ['row', 'collapsed']);
  assert.equal(views[0].items[1].steps, 1);
  assert.equal(views[0].items[1].tools, 0);
  assert.equal(views[1].items[1].steps, 2);
  assert.equal(views[1].items[1].tools, 1);

  const expanded = toggleTraceTurn({ ...createWorkspaceState('a', null), traceFilter: 'turn' }, 1);
  assert.equal(expanded.traceFilter, 'none');
  assert.deepEqual(expanded.expandedTurns, []);
});
