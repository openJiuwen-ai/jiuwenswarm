import assert from 'node:assert/strict';
import test from 'node:test';

import {
  latestModifiedTurn,
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
  files: { 'src/a.py': {} },
  ...overrides,
});

test('selects only the latest file-editing turn regardless of pure chat turns', () => {
  const latest = turn({ turn_index: 2 });
  const chats = Array.from({ length: 100 }, (_, index) => turn({ turn_index: index + 3, files: {}, stats: { files_changed: 0, lines_added: 0, lines_removed: 0 } }));
  assert.equal(latestModifiedTurn([turn(), latest, ...chats]), latest);
  assert.equal(latestModifiedTurn(chats), null);
});

test('discard keeps the same target and a new file edit replaces it', () => {
  const discarded = turn({ status: 'discarded' });
  assert.equal(latestModifiedTurn([discarded, turn({ turn_index: 2, files: {}, stats: { files_changed: 0, lines_added: 0, lines_removed: 0 } })]), discarded);
  const newer = turn({ turn_index: 3, change_set_id: 'cs-3' });
  assert.equal(latestModifiedTurn([newer, discarded]), newer);
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

test('localizes diff history expired errors', () => {
  const error = Object.assign(new Error('raw backend message'), { code: 'DIFF_HISTORY_EXPIRED' });
  assert.equal(turnChangeErrorMessage(error, 'discard'), '修改记录已过期，无法定位该轮修改，请刷新后重试');
});
