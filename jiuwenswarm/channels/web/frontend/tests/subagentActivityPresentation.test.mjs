import assert from 'node:assert/strict';
import test from 'node:test';

import {
  extractSubagentTasks,
  finalizeSubagentTasks,
  getSubagentActivityPreview,
  groupSubagentActivities,
} from '../node_modules/.cache/subagent-activity/subagentActivityPresentation.mjs';

function activity(activityId, sequence, overrides = {}) {
  return {
    activity_id: activityId,
    subagent_id: 'agent-a',
    task_id: 'task-a',
    sequence,
    kind: 'thinking',
    summary: '',
    at_ms: 1000 + sequence,
    ...overrides,
  };
}

test('groups adjacent thinking stream chunks and keeps one bounded preview', () => {
  const groups = groupSubagentActivities([
    activity('thinking-1', 1, { summary: 'The' }),
    activity('thinking-2', 2, { summary: ' user wants' }),
    activity('thinking-3', 3, { summary: ' a report.' }),
  ]);

  assert.equal(groups.length, 1);
  assert.equal(groups[0].summary, 'The user wants a report.');
  assert.equal(groups[0].count, 3);
  assert.deepEqual(groups[0].activities.map(item => item.activity_id), [
    'thinking-1',
    'thinking-2',
    'thinking-3',
  ]);
  assert.equal(getSubagentActivityPreview(groups[0]), 'The user wants a report.');
});

test('does not merge thinking across task boundaries or non-thinking activities', () => {
  const groups = groupSubagentActivities([
    activity('thinking-1', 1, { summary: 'first' }),
    activity('tool-1', 2, { kind: 'tool_call', tool_name: 'search', summary: 'search()' }),
    activity('thinking-2', 3, { summary: 'second' }),
    activity('thinking-3', 4, { task_id: 'task-b', summary: 'new task' }),
  ]);

  assert.deepEqual(groups.map(group => [group.activity.kind, group.activity.task_id, group.summary]), [
    ['thinking', 'task-a', 'first'],
    ['tool_call', 'task-a', 'search()'],
    ['thinking', 'task-a', 'second'],
    ['thinking', 'task-b', 'new task'],
  ]);
});

test('keeps thinking phases separate when tool activities are interleaved', () => {
  const groups = groupSubagentActivities([
    activity('thinking-1', 1, { summary: 'first thought' }),
    activity('tool-1', 2, { kind: 'tool_call', tool_name: 'search', summary: 'search()' }),
    activity('thinking-2', 3, { summary: 'second thought' }),
  ]);

  assert.deepEqual(groups.map(group => [group.activity.kind, group.summary, group.count]), [
    ['thinking', 'first thought', 1],
    ['tool_call', 'search()', 1],
    ['thinking', 'second thought', 1],
  ]);
});

test('merges thinking by phase while keeping other activities as sibling rows', () => {
  const groups = groupSubagentActivities([
    activity('thinking-1', 1, { phase_id: 1, summary: 'phase one start' }),
    activity('tool-1', 2, { phase_id: 1, kind: 'tool_call', tool_name: 'search', summary: 'search()' }),
    activity('thinking-2', 3, { phase_id: 1, summary: 'phase one continuation' }),
    activity('thinking-3', 4, { phase_id: 2, summary: 'phase two' }),
  ]);

  assert.deepEqual(groups.map(group => [group.activity.kind, group.summary, group.count]), [
    ['thinking', 'phase one startphase one continuation', 2],
    ['tool_call', 'search()', 1],
    ['thinking', 'phase two', 1],
  ]);
});

test('keeps todo creation and result visible in the activity timeline', () => {
  const groups = groupSubagentActivities([
    activity('thinking-1', 1, { summary: '先规划' }),
    activity('call-1', 1, {
      kind: 'tool_call',
      tool_name: 'todo_create',
      tool_call_id: 'tool-1',
      summary: 'todo_create({"tasks":[{"id":"research","content":"搜索市场"},{"id":"write","content":"撰写报告"}]})',
    }),
    activity('result-1', 2, {
      kind: 'tool_result',
      tool_name: 'todo_create',
      tool_call_id: 'tool-1',
      summary: "[→] task_id: research , content: 搜索市场\n[ ] task_id: write , content: 撰写报告",
    }),
    activity('thinking-2', 3, { summary: '继续执行' }),
  ]);

  assert.deepEqual(groups.map(group => group.activity.kind), [
    'thinking',
    'tool_call',
    'tool_result',
    'thinking',
  ]);
  assert.match(groups[1].summary, /todo_create/);
  assert.match(groups[2].summary, /search|task_id/);
  assert.deepEqual(extractSubagentTasks([
    activity('call-1', 1, {
      kind: 'tool_call',
      tool_name: 'todo_create',
      summary: 'todo_create({"tasks":[{"id":"research","content":"搜索市场"},{"id":"write","content":"撰写报告"}]})',
    }),
    activity('result-1', 2, {
      kind: 'tool_result',
      tool_name: 'todo_create',
      summary: "[→] task_id: research , content: 搜索市场\n[ ] task_id: write , content: 撰写报告",
    }),
  ]).map(({ id, content, status }) => ({ id, content, status })), [
    { id: 'research', content: '搜索市场', status: 'in_progress' },
    { id: 'write', content: '撰写报告', status: 'pending' },
  ]);
});

test('applies todo_modify status updates in the task section', () => {
  const tasks = extractSubagentTasks([
    activity('call-todo', 1, {
      kind: 'tool_call',
      tool_name: 'todo_create',
      tool_call_id: 'create-1',
      summary: 'todo_create({"tasks":[{"id":"research_report","content":"撰写市场报告"}]})',
    }),
    activity('create-result', 2, {
      kind: 'tool_result',
      tool_name: 'todo_create',
      tool_call_id: 'create-1',
      summary: '[→] task_id: research_report , content: 撰写市场报告',
    }),
    activity('modify-todo-call', 3, {
      kind: 'tool_call',
      tool_name: 'todo_modify',
      tool_call_id: 'modify-1',
      summary: 'todo_modify({"todos":[{"id":"research_report","status":"completed"}]})',
    }),
    activity('modify-todo', 4, {
      kind: 'tool_result',
      tool_name: 'todo_modify',
      tool_call_id: 'modify-1',
      summary: "{'message': 'Successfully updated 1 task(s)'}",
    }),
  ]);
  assert.deepEqual(tasks.map(({ id, content, status }) => ({ id, content, status })), [
    { id: 'research_report', content: '撰写市场报告', status: 'completed' },
  ]);
  assert.deepEqual(tasks[0].statusHistory.map(change => change.status), [
    'pending',
    'in_progress',
    'completed',
  ]);
});

test('ignores failed todo updates and closes outstanding tasks only on a completed turn', () => {
  const tasks = extractSubagentTasks([
    activity('create-call', 1, {
      kind: 'tool_call',
      tool_name: 'todo_create',
      tool_call_id: 'create-2',
      summary: 'todo_create({"tasks":[{"id":"one","content":"第一项"},{"id":"two","content":"第二项"}]})',
    }),
    activity('create-result', 2, {
      kind: 'tool_result',
      tool_name: 'todo_create',
      tool_call_id: 'create-2',
      summary: '[→] task_id: one , content: 第一项\n[ ] task_id: two , content: 第二项',
    }),
    activity('failed-call', 3, {
      kind: 'tool_call',
      tool_name: 'todo_modify',
      tool_call_id: 'failed-1',
      summary: 'todo_modify({"todos":[{"id":"one","status":"completed"},{"id":"two","status":"in_progress"}]})',
    }),
    activity('failed-result', 4, {
      kind: 'tool_result',
      tool_name: 'todo_modify',
      tool_call_id: 'failed-1',
      summary: 'success=False data=None error="validation failed"',
    }),
  ]);

  assert.deepEqual(tasks.map(({ id, status }) => ({ id, status })), [
    { id: 'one', status: 'in_progress' },
    { id: 'two', status: 'pending' },
  ]);
  assert.deepEqual(finalizeSubagentTasks(tasks, true).map(({ id, status }) => ({ id, status })), [
    { id: 'one', status: 'completed' },
    { id: 'two', status: 'completed' },
  ]);
  assert.deepEqual(finalizeSubagentTasks(tasks, false).map(({ id, status }) => ({ id, status })), [
    { id: 'one', status: 'in_progress' },
    { id: 'two', status: 'pending' },
  ]);
});

test('uses the meaningful result content as the collapsed web-tool preview', () => {
  const groups = groupSubagentActivities([
    activity('call-search', 1, {
      kind: 'tool_call',
      tool_name: 'fetch_webpage',
      tool_call_id: 'web-1',
      summary: 'fetch_webpage({"url":"https://example.com/report","max_chars":15000})',
    }),
    activity('result-search', 2, {
      kind: 'tool_result',
      tool_name: 'fetch_webpage',
      tool_call_id: 'web-1',
      summary: 'URL: https://example.com/report\nStatus: 200\nContent: 智能眼镜市场报告摘要，包含50个结果',
    }),
  ]);

  assert.equal(getSubagentActivityPreview(groups[1]), '智能眼镜市场报告摘要，包含50个结果');
});

test('keeps thinking collapsed preview bounded and empty for empty summaries', () => {
  const groups = groupSubagentActivities([
    activity('thinking-1', 1, { summary: '先拆解任务\n再检查输入' }),
  ]);

  assert.equal(getSubagentActivityPreview(groups[0]), '先拆解任务');
  assert.equal(getSubagentActivityPreview(groupSubagentActivities([
    activity('thinking-long', 3, { summary: 'x'.repeat(200) }),
  ])[0]).length, 120);
  assert.equal(getSubagentActivityPreview(groupSubagentActivities([
    activity('thinking-empty', 2, { summary: '' }),
  ])[0]), '');
});
