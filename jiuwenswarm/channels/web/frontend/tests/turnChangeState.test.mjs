import assert from 'node:assert/strict';
import test from 'node:test';

import {
  latestTurnDiffKey,
  latestTurnDiffKeyForMessages,
  turnChangeErrorMessage,
  updateTurnChangeStatus,
} from '../node_modules/.cache/turn-change-state/features/code-mode/turnChangeState.js';

const turn = (overrides = {}) => ({
  kind: 'conversation_turn',
  change_set_id: 'cs-1',
  turn_index: 1,
  request_id: 'req-1',
  user_message_id: 'user-1',
  assistant_message_id: 'assistant-1',
  timestamp: '2026-08-04T10:00:00.000Z',
  user_prompt_preview: 'edit files',
  status: 'completed',
  stats: { files_changed: 1, lines_added: 2, lines_removed: 1 },
  files: {},
  ...overrides,
});

test('only the newest turn diff is exposed as the undo or redo target', () => {
  assert.equal(
    latestTurnDiffKey([
      turn({ change_set_id: 'cs-4', turn_index: 4 }),
      turn({ change_set_id: 'cs-2', turn_index: 2 }),
      turn({ change_set_id: 'cs-7', turn_index: 7 }),
    ]),
    'cs-7',
  );
});

test('hides the action when the latest user turn did not produce a diff', () => {
  assert.equal(latestTurnDiffKey([turn({ user_message_id: 'user-1' })], 'user-2'), null);
});

test('uses the latest rendered card when the live user id differs from persisted history', () => {
  const latestTurn = turn({ user_message_id: 'persisted-user-1' });
  const messages = [
    { id: 'user-frontend-1', role: 'user' },
    { id: 'assistant-live-1', role: 'assistant' },
  ];
  const bindings = new Map([['assistant-live-1', [latestTurn]]]);

  assert.equal(latestTurnDiffKeyForMessages(messages, [latestTurn], bindings), 'cs-1');
});

test('does not expose an older bound card when the newest user turn has no diff', () => {
  const previousTurn = turn({ user_message_id: 'persisted-user-1' });
  const messages = [
    { id: 'user-frontend-1', role: 'user' },
    { id: 'assistant-live-1', role: 'assistant' },
    { id: 'user-frontend-2', role: 'user' },
    { id: 'assistant-live-2', role: 'assistant' },
  ];
  const bindings = new Map([['assistant-live-1', [previousTurn]]]);

  assert.equal(latestTurnDiffKeyForMessages(messages, [previousTurn], bindings), null);
});

test('updates the operation target by change set id', () => {
  const turns = [turn(), turn({ change_set_id: 'cs-2', turn_index: 2 })];
  const updated = updateTurnChangeStatus(turns, { change_set_id: 'cs-2', turn_index: 2 }, 'discarded');

  assert.equal(updated[0].status, 'completed');
  assert.equal(updated[1].status, 'discarded');
});

test('falls back to the turn index when the backend returns no change set id', () => {
  const updated = updateTurnChangeStatus([turn()], { change_set_id: null, turn_index: 1 }, 'discarded');
  assert.equal(updated[0].status, 'discarded');
});

test('localizes backend redo history errors', () => {
  const error = Object.assign(new Error('raw backend message'), { code: 'REDO_HISTORY_MISSING' });
  assert.equal(turnChangeErrorMessage(error, 'redo'), '撤销记录不完整，无法重新应用修改');
});
