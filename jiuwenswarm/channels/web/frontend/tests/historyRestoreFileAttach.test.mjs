import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  canMessageHostFileItems,
  parseHistoryJsonFileToTimelinePreview,
} from '../node_modules/.cache/history-restore-file-attach/historyRestore.mjs';

const SESSION = 'sess';
const T = 1_790_000_000;

const user = (id, ts, content) => ({ id, role: 'user', request_id: id, timestamp: ts, content });
const final = (id, ts, content, extra = {}) => ({
  id,
  role: 'assistant',
  request_id: id,
  timestamp: ts,
  event_type: 'chat.final',
  content,
  ...extra,
});
const file = (id, ts, names) => ({
  id,
  role: 'assistant',
  request_id: id,
  timestamp: ts,
  event_type: 'chat.file',
  content: '',
  files: names.map((name) => ({ name, path: `/tmp/${name}`, size: 1 })),
});

const restore = (records) => parseHistoryJsonFileToTimelinePreview(records, SESSION).messages;
const names = (message) => (message.fileItems ?? []).map((item) => item.name);

test('restored chat.file stays in its own turn instead of the previous assistant', () => {
  const messages = restore([
    user('u1', T, '第一轮：写个说明'),
    final('f1', T + 1, '第一轮总结'),
    file('f1-file', T + 2, ['第一轮产物.txt']),
    user('u2', T + 3, '第二轮：生成文档'),
    file('f2-file', T + 4, ['报告.docx']),
    final('f2', T + 5, '第二轮总结'),
  ]);

  const firstTurnFinal = messages.find((message) => message.content === '第一轮总结');
  assert.deepEqual(names(firstTurnFinal), ['第一轮产物.txt']);

  const secondTurnFile = messages.find((message) => message.id.startsWith('hist-file-'));
  assert.ok(secondTurnFile, 'second-turn file should be restored as its own card');
  assert.deepEqual(names(secondTurnFile), ['报告.docx']);
  assert.ok(
    messages.indexOf(secondTurnFile) > messages.findIndex((message) => message.content === '第二轮：生成文档'),
    'file card must sit after the second user message',
  );
  assert.ok(
    messages.indexOf(secondTurnFile) < messages.findIndex((message) => message.content === '第二轮总结'),
    'file card must sit before the second final',
  );

  const secondTurnFinal = messages.find((message) => message.content === '第二轮总结');
  assert.equal(secondTurnFinal.fileItems, undefined);
  assert.equal(messages.filter((message) => message.fileItems?.length).length, 2);
});

test('restored chat.file attaches to the assistant text that precedes it, not the later final', () => {
  const messages = restore([
    user('u1', T, '第一轮'),
    final('f1', T + 1, '第一轮总结'),
    user('u2', T + 2, '第二轮：生成文档'),
    final('fa', T + 3, '好的，我来生成'),
    file('fa-file', T + 4, ['报告.docx']),
    final('fb', T + 5, '第二轮总结'),
  ]);

  const before = messages.find((message) => message.content === '好的，我来生成');
  const after = messages.find((message) => message.content === '第二轮总结');
  assert.deepEqual(names(before), ['报告.docx']);
  assert.equal(after.fileItems, undefined);
  assert.equal(messages.some((message) => message.id.startsWith('hist-file-')), false);
  assert.ok(messages.indexOf(before) < messages.indexOf(after));
});

test('a file at the end of a user turn is not dropped and stays last', () => {
  const messages = restore([
    user('u1', T, '第一轮'),
    final('f1', T + 1, '第一轮总结'),
    user('u2', T + 2, '第二轮：再生成一次'),
    file('tail-file', T + 3, ['末尾文件.txt']),
  ]);

  const last = messages[messages.length - 1];
  assert.ok(last.id.startsWith('hist-file-'));
  assert.deepEqual(names(last), ['末尾文件.txt']);
  assert.equal(messages.find((message) => message.content === '第一轮总结').fileItems, undefined);
});

test('multiple files after the same host merge in arrival order', () => {
  const messages = restore([
    user('u1', T, '生成两个文件'),
    final('fa', T + 1, '开始生成'),
    file('a', T + 2, ['a.txt']),
    file('b', T + 3, ['b.txt']),
    final('fb', T + 4, '生成完成'),
  ]);

  assert.deepEqual(names(messages.find((message) => message.content === '开始生成')), ['a.txt', 'b.txt']);
  assert.equal(messages.some((message) => message.id.startsWith('hist-file-')), false);
});

test('a file after the turn final attaches to that final', () => {
  const messages = restore([
    user('u1', T, '生成'),
    final('fa', T + 1, '中间说明'),
    final('fb', T + 2, '最终总结'),
    file('f', T + 3, ['final-after.txt']),
  ]);

  assert.deepEqual(names(messages.find((message) => message.content === '最终总结')), ['final-after.txt']);
  assert.equal(messages.find((message) => message.content === '中间说明').fileItems, undefined);
});

test('a file before the turn assistant text and a later file both land in the same turn', () => {
  const messages = restore([
    user('u1', T, '第一轮'),
    final('f1', T + 1, '第一轮总结'),
    user('u2', T + 2, '第二轮：先生成文件再解释'),
    file('first', T + 3, ['先到的文件.txt']),
    final('fa', T + 4, '文件已生成'),
    file('second', T + 5, ['后到的文件.txt']),
    final('fb', T + 6, '第二轮总结'),
  ]);

  const standalone = messages.find((message) => message.id.startsWith('hist-file-'));
  assert.ok(standalone);
  assert.deepEqual(names(standalone), ['先到的文件.txt']);
  assert.deepEqual(names(messages.find((message) => message.content === '文件已生成')), ['后到的文件.txt']);
  const fileIndex = messages.indexOf(standalone);
  assert.ok(fileIndex > messages.findIndex((message) => message.content === '第二轮：先生成文件再解释'));
  assert.ok(fileIndex < messages.findIndex((message) => message.content === '文件已生成'));
});

test('a file before any message becomes a standalone card', () => {
  const messages = restore([file('only', T, ['only.txt'])]);

  assert.equal(messages.length, 1);
  assert.equal(messages[0].role, 'assistant');
  assert.ok(messages[0].id.startsWith('hist-file-'));
  assert.deepEqual(names(messages[0]), ['only.txt']);
});

test('a file after a system message becomes a standalone card', () => {
  const messages = restore([
    user('u1', T, '出错'),
    {
      id: 'err',
      role: 'assistant',
      request_id: 'err',
      timestamp: T + 1,
      event_type: 'chat.error',
      content: 'boom',
    },
    file('f', T + 2, ['after-error.txt']),
  ]);

  const errorMessage = messages.find((message) => message.id === 'err');
  assert.equal(errorMessage.role, 'system');
  assert.equal(errorMessage.fileItems, undefined);
  const last = messages[messages.length - 1];
  assert.ok(last.id.startsWith('hist-file-'));
  assert.deepEqual(names(last), ['after-error.txt']);
});

test('a file after a goal completion card becomes a standalone card', () => {
  const messages = restore([
    user('u1', T, '第一轮'),
    final('f1', T + 1, '第一轮总结'),
    user('u2', T + 2, '设个目标'),
    final('g', T + 3, 'goal.completed:{"evidence":"目标达成"}'),
    file('f', T + 4, ['目标产物.txt']),
  ]);

  const goalCard = messages.find((message) => message.id === 'g');
  assert.ok(goalCard);
  assert.equal(goalCard.content.startsWith('goal.completed:'), true);
  assert.equal(goalCard.fileItems, undefined);

  const standalone = messages.find((message) => message.id.startsWith('hist-file-'));
  assert.ok(standalone);
  assert.deepEqual(names(standalone), ['目标产物.txt']);
  assert.ok(
    messages.indexOf(standalone) > messages.findIndex((message) => message.content === '设个目标'),
    'file card must sit after the second user message',
  );
  assert.ok(
    messages.indexOf(standalone) < messages.indexOf(goalCard),
    'file card must sit before the goal card that is sunk to the turn end',
  );
  assert.equal(messages.find((message) => message.content === '第一轮总结').fileItems, undefined);
});

test('team leader bubbles still host file cards', () => {
  const messages = restore([
    user('u1', T, '团队任务'),
    final('f1', T + 1, 'leader 回复', { mode: 'team' }),
    file('f', T + 2, ['团队产物.txt']),
  ]);

  const leader = messages.find((message) => message.id.startsWith('team-leader-'));
  assert.ok(leader);
  assert.deepEqual(names(leader), ['团队产物.txt']);
  assert.equal(messages.some((message) => message.id.startsWith('hist-file-')), false);
});

test('team mode without a preceding leader bubble falls back to a standalone card', () => {
  // 已知限制：实时 team 模式会新建 team-leader 气泡，恢复侧只能新建普通 assistant 文件消息。
  const messages = restore([
    user('u1', T, '团队任务'),
    final('f1', T + 1, '上一轮 leader 回复', { mode: 'team' }),
    user('u2', T + 2, '再生成一个文件'),
    file('f', T + 3, ['团队产物.txt']),
  ]);

  const leader = messages.find((message) => message.id.startsWith('team-leader-'));
  assert.equal(leader.fileItems, undefined);
  const standalone = messages[messages.length - 1];
  assert.ok(standalone.id.startsWith('hist-file-'));
  assert.equal(standalone.role, 'assistant');
  assert.deepEqual(names(standalone), ['团队产物.txt']);
});

test('only bubbles that render file items can host chat.file', () => {
  assert.equal(canMessageHostFileItems({ role: 'assistant', id: 'a', content: 'hi' }), true);
  assert.equal(canMessageHostFileItems({ role: 'assistant', id: 'qa', content: 'qa.summary:{}' }), true);
  assert.equal(
    canMessageHostFileItems({ role: 'system', id: 'team-leader-1', content: 'team.leader:{}' }),
    true,
  );
  assert.equal(
    canMessageHostFileItems({ role: 'assistant', id: 'goal', content: 'goal.completed:{}' }),
    false,
  );
  assert.equal(canMessageHostFileItems({ role: 'system', id: 'sys', content: 'x' }), false);
  assert.equal(canMessageHostFileItems({ role: 'user', id: 'u', content: 'x' }), false);
  assert.equal(canMessageHostFileItems(undefined), false);
});
