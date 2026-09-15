import assert from 'node:assert/strict';
import test from 'node:test';

import { bindTurnDiffsToMessages } from '../node_modules/.cache/code-turn-diff-binding/features/code-mode/codeTurnDiffBinding.js';

const turn = (overrides = {}) => ({
  kind: 'conversation_turn',
  turn_index: 1,
  timestamp: '2026-07-23T10:00:01.000Z',
  user_prompt_preview: 'write code',
  stats: { files_changed: 1, lines_added: 2, lines_removed: 0 },
  files: { 'a.py': {} },
  change_set_id: 'cs-1',
  request_id: 'req-1',
  assistant_message_id: 'assistant-1',
  user_message_id: 'user-1',
  status: 'completed',
  ...overrides,
});

test('binds team agent p2p output to the turn diff card anchor', () => {
  const teamAgentMessage = {
    id: 'team-message-1',
    role: 'system',
    content: `team.event:${JSON.stringify({
      event: {
        type: 'team.message.p2p',
        from_member: 'code_agent',
        to_member: 'user',
        content: 'done',
        message_id: 'member-msg-1',
        timestamp: Date.now(),
      },
    })}`,
    timestamp: '2026-07-23T10:00:02.000Z',
  };

  const result = bindTurnDiffsToMessages([
    {
      id: 'user-1',
      role: 'user',
      content: 'write code',
      timestamp: '2026-07-23T10:00:00.000Z',
    },
    teamAgentMessage,
  ], [turn()]);

  assert.deepEqual(result.get('team-message-1'), [turn()]);
});

test('binds team event when payload content contains the team event prefix', () => {
  const result = bindTurnDiffsToMessages([
    {
      id: 'user-1',
      role: 'user',
      content: 'write code',
      timestamp: '2026-07-23T10:00:00.000Z',
    },
    {
      id: 'team-message-prefix-content',
      role: 'system',
      content: `team.event:${JSON.stringify({
        event: {
          type: 'team.message.p2p',
          from_member: 'code_agent',
          to_member: 'user',
          content: 'literal marker team.event: should stay inside JSON',
          message_id: 'member-msg-prefix',
          timestamp: Date.now(),
        },
      })}`,
      timestamp: '2026-07-23T10:00:02.000Z',
    },
  ], [turn()]);

  assert.deepEqual(result.get('team-message-prefix-content'), [turn()]);
});

test('does not bind hidden team collaboration broadcasts as visible anchors', () => {
  const result = bindTurnDiffsToMessages([
    {
      id: 'user-1',
      role: 'user',
      content: 'write code',
      timestamp: '2026-07-23T10:00:00.000Z',
    },
    {
      id: 'team-message-hidden',
      role: 'system',
      content: `team.event:${JSON.stringify({
        event: {
          type: 'team.message.broadcast',
          from_member: 'code_agent',
          content: 'internal update',
        },
      })}`,
      timestamp: '2026-07-23T10:00:02.000Z',
    },
  ], [turn()]);

  assert.equal(result.size, 0);
});

test('binds a cron final broadcast to its persisted turn diff', () => {
  const cronTurn = turn({
    request_id: 'cron-nightly:1720000000',
    user_message_id: 'cron-nightly:1720000000:user',
    assistant_message_id: 'cron-nightly:1720000000:assistant',
  });
  const result = bindTurnDiffsToMessages([
    {
      id: 'cron-final-nightly:1720000000',
      role: 'assistant',
      content: 'done',
      timestamp: '2026-07-23T10:00:02.000Z',
    },
  ], [cronTurn]);

  assert.deepEqual(result.get('cron-final-nightly:1720000000'), [cronTurn]);
});

const chatMessages = (index) => [
  { id: 'user-' + index, role: 'user', content: 'chat', timestamp: new Date(Date.UTC(2026, 6, 23, 10, index)).toISOString() },
  { id: 'assistant-' + index, role: 'assistant', content: 'reply', timestamp: new Date(Date.UTC(2026, 6, 23, 10, index, 2)).toISOString() },
];

test('only the newest editing turn has a card and pure chats do not move it', () => {
  const older = turn();
  const latest = turn({ turn_index: 2, change_set_id: 'cs-2', user_message_id: 'user-2', assistant_message_id: 'assistant-2' });
  const messages = Array.from({ length: 100 }, (_, index) => chatMessages(index + 1)).flat();
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [older, latest])], [['assistant-2', [latest]]]);
  latest.status = 'discarded';
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [older, latest])], [['assistant-2', [latest]]]);
  const newer = turn({ turn_index: 3, change_set_id: 'cs-3', user_message_id: 'user-3', assistant_message_id: 'assistant-3' });
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [older, latest, newer])], [['assistant-3', [newer]]]);
});

test('live temporary ids bind by turn time and stay fixed after pure chats', () => {
  const latest = turn({ timestamp: '2026-07-23T10:02:01.000Z', user_message_id: 'persisted-user', assistant_message_id: 'persisted-assistant' });
  const messages = [chatMessages(2), chatMessages(3), chatMessages(4)].flat();
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [latest])], [['assistant-2', [latest]]]);
});

test('a fast browser clock still binds the turn own user message', () => {
  // Live timestamps come from the browser clock: 90s ahead of the server, the
  // turn own user message (10:03:31) lands after the persisted turn start
  // (10:02:01) while the previous round is more than the skew budget older.
  const latest = turn({ timestamp: '2026-07-23T10:02:01.000Z', user_message_id: 'persisted-user', assistant_message_id: 'persisted-assistant' });
  const messages = [
    ...chatMessages(0),
    { id: 'user-live', role: 'user', content: 'edit files', timestamp: new Date(Date.UTC(2026, 6, 23, 10, 3, 31)).toISOString() },
    { id: 'assistant-live', role: 'assistant', content: 'done', timestamp: new Date(Date.UTC(2026, 6, 23, 10, 3, 33)).toISOString() },
  ];
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [latest])], [['assistant-live', [latest]]]);
});

test('a fast browser clock binds the first modified turn instead of dropping the card', () => {
  const latest = turn({ timestamp: '2026-07-23T10:02:01.000Z', user_message_id: 'persisted-user', assistant_message_id: 'persisted-assistant' });
  const messages = [
    { id: 'user-live', role: 'user', content: 'edit files', timestamp: new Date(Date.UTC(2026, 6, 23, 10, 3, 31)).toISOString() },
    { id: 'assistant-live', role: 'assistant', content: 'done', timestamp: new Date(Date.UTC(2026, 6, 23, 10, 3, 33)).toISOString() },
  ];
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [latest])], [['assistant-live', [latest]]]);
});

test('a quick follow-up chat never steals the card under synced clocks', () => {
  // The turn own message sits right before the turn start; the next chat round
  // arrives inside the skew budget but is a later round — the at-or-before
  // candidate must win.
  const latest = turn({ timestamp: '2026-07-23T10:02:01.000Z', user_message_id: 'persisted-user', assistant_message_id: 'persisted-assistant' });
  const messages = [
    { id: 'user-live', role: 'user', content: 'edit files', timestamp: new Date(Date.UTC(2026, 6, 23, 10, 2, 0)).toISOString() },
    { id: 'assistant-live', role: 'assistant', content: 'done', timestamp: new Date(Date.UTC(2026, 6, 23, 10, 2, 30)).toISOString() },
    ...chatMessages(3),
  ];
  assert.deepEqual([...bindTurnDiffsToMessages(messages, [latest])], [['assistant-live', [latest]]]);
});

test('an off-screen editing turn never borrows a paginated conversation bubble', () => {
  const latest = turn({ turn_index: 2, timestamp: '2026-07-23T10:02:01.000Z', user_message_id: 'user-2', assistant_message_id: 'assistant-2' });
  const messages = [chatMessages(5), chatMessages(6), chatMessages(7)].flat();
  assert.equal(bindTurnDiffsToMessages(messages, [latest]).size, 0);
});
