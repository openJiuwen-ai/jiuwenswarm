import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.localStorage = globalThis.window.localStorage;

const { useChatStore } = await import('../node_modules/.cache/history-restore-file-attach/chatStore.mjs');

const chat = () => useChatStore.getState();
const messagesOf = (sessionId) => chat().getRuntime(sessionId).messages;
const names = (message) => (message.fileItems ?? []).map((item) => item.name);
const file = (name) => ({ name, path: `/tmp/${name}`, size: 1 });

function seed(sessionId, messages) {
  chat().ensureRuntime(sessionId);
  chat().replaceHistoryMessages(sessionId, messages);
}

test('live chat.file attaches to the last assistant message', () => {
  const sid = 'live_file_attach_assistant';
  seed(sid, []);
  chat().addMessage(sid, {
    id: 'reply',
    role: 'assistant',
    content: 'done',
    timestamp: '2026-09-22T00:00:00.000Z',
  });
  chat().addFileItems(sid, [file('a.txt')], { timestampIso: '2026-09-22T00:00:01.000Z' });

  const messages = messagesOf(sid);
  assert.equal(messages.length, 1);
  assert.equal(messages[0].id, 'reply');
  assert.deepEqual(names(messages[0]), ['a.txt']);
});

test('live chat.file does not hide on a goal completion card', () => {
  const sid = 'live_file_attach_goal';
  seed(sid, []);
  chat().addMessage(sid, {
    id: 'goal',
    role: 'assistant',
    content: 'goal.completed:{"evidence":"ok"}',
    timestamp: '2026-09-22T00:00:00.000Z',
  });
  chat().addFileItems(sid, [file('b.txt')], { timestampIso: '2026-09-22T00:00:01.000Z' });

  const messages = messagesOf(sid);
  assert.equal(messages.length, 2);
  assert.equal(messages[0].fileItems, undefined);
  assert.equal(messages[1].id, 'file-2026-09-22T00:00:01.000Z');
  assert.deepEqual(names(messages[1]), ['b.txt']);
});

test('live chat.file still attaches to a team leader bubble', () => {
  const sid = 'live_file_attach_team_leader';
  seed(sid, []);
  chat().addMessage(sid, {
    id: 'team-leader-1',
    role: 'system',
    content: 'team.leader:{}',
    timestamp: '2026-09-22T00:00:00.000Z',
  });
  chat().addFileItems(sid, [file('d.txt')], { timestampIso: '2026-09-22T00:00:01.000Z' });

  const messages = messagesOf(sid);
  assert.equal(messages.length, 1);
  assert.deepEqual(names(messages[0]), ['d.txt']);
});

test('live chat.file creates its own card when the last message is a user message', () => {
  const sid = 'live_file_attach_user';
  seed(sid, []);
  chat().addMessage(sid, {
    id: 'u1',
    role: 'user',
    content: '生成文件',
    timestamp: '2026-09-22T00:00:00.000Z',
  });
  chat().addFileItems(sid, [file('e.txt')], { timestampIso: '2026-09-22T00:00:01.000Z' });

  const messages = messagesOf(sid);
  assert.equal(messages.length, 2);
  assert.equal(messages[0].fileItems, undefined);
  assert.ok(messages[1].id.startsWith('file-'));
  assert.deepEqual(names(messages[1]), ['e.txt']);
});
