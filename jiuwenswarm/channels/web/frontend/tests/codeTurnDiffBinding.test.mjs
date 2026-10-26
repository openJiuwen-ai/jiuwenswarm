import assert from 'node:assert/strict';
import test from 'node:test';

import { bindTurnDiffsToMessages } from '../node_modules/.cache/code-turn-diff-binding/features/code-mode/codeTurnDiffBinding.js';

const turn = (overrides = {}) => ({
  kind: 'conversation_turn',
  turn_index: 1,
  timestamp: '2026-07-23T10:00:01.000Z',
  user_prompt_preview: 'write code',
  stats: { files_changed: 1, lines_added: 2, lines_removed: 0 },
  files: {},
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
