import assert from 'node:assert/strict';
import test from 'node:test';
import {
  buildRenderItems,
  buildTurnWorkMeta,
  buildTurnFoldAnchorKeys,
  buildLiveCompletedStreaks,
} from '../node_modules/.cache/build-turn-timeline/buildTurnTimeline.js';
import { projectTimelineItems } from '../node_modules/.cache/build-turn-timeline/projectTimelineItems.js';

const epoch = 1_700_000_000_000;
const iso = (offset) => new Date(epoch + offset).toISOString();
function message(id, role, offset) {
  return {
    type: 'message',
    key: id,
    timestampMs: epoch + offset,
    sourceIndex: offset,
    message: { id, renderKey: id, role, content: id, timestamp: iso(offset), completedAt: iso(offset) },
  };
}
function longTurn(count = 240) {
  const items = [message('user', 'user', 0)];
  for (let index = 0; index < count; index++) {
    const at = index * 10 + 1;
    items.push({
      type: 'reasoning',
      key: `reasoning-${index}`,
      timestampMs: epoch + at,
      sourceIndex: at,
      segment: {
        id: `reasoning-${index}`,
        text: 'thinking',
        startedAt: epoch + at,
        closed: true,
        closedAt: epoch + at,
      },
    });
    items.push({
      type: 'toolExecution',
      key: `tool-${index}`,
      timestampMs: epoch + at + 1,
      sourceIndex: at + 1,
      execution: {
        toolCallId: `tool-${index}`,
        toolCall: { name: 'read_file', arguments: {} },
        status: 'completed',
        startedAt: iso(at + 1),
        updatedAt: iso(at + 2),
      },
    });
    if (index === 20) items.push(message('progress', 'assistant', at + 3));
  }
  items.push(message('answer', 'assistant', count * 10 + 10));
  return items;
}
function project(input, expandedTurns = {}, expandedStreaks = {}, processing = false) {
  const items = buildRenderItems(input, false, processing);
  const meta = buildTurnWorkMeta(items, processing);
  const streaks = buildLiveCompletedStreaks(items, epoch + 1_000_000);
  return {
    items,
    streaks,
    rows: projectTimelineItems(
      items,
      meta,
      buildTurnFoldAnchorKeys(items, meta),
      streaks,
      expandedTurns,
      expandedStreaks,
    ),
  };
}

test('a long folded turn retains every message and its work header without mounting hidden work', () => {
  const { rows, items } = project(longTurn(2_000));
  assert.ok(items.length > 4_000);
  assert.deepEqual(
    rows.filter((row) => row.item.type === 'message').map((row) => row.item.message.id),
    ['user', 'progress', 'answer'],
  );
  assert.equal(rows.length, 4);
  assert.equal(rows.filter((row) => row.isTurnAnchor).length, 1);
  assert.ok(rows.every((row) => row.item.type === 'message' || !row.contentOpen));
});

test('turn and streak expansion exposes all work, with stable header and message identities', () => {
  const input = longTurn();
  const folded = project(input);
  const header = folded.rows.find((row) => row.isTurnAnchor);
  const expandedTurns = { [header.turnKey]: true };
  const turnOpen = project(input, expandedTurns);
  assert.equal(turnOpen.rows.filter((row) => row.streak?.firstKey === row.item.key).length, folded.streaks.size);
  assert.equal(turnOpen.rows.find((row) => row.isTurnAnchor).key, header.key);
  const expandedStreaks = Object.fromEntries([...folded.streaks.values()].map((streak) => [streak.id, true]));
  const allOpen = project(input, expandedTurns, expandedStreaks);
  assert.equal(allOpen.rows.filter((row) => row.contentOpen && row.item.type !== 'message').length, 480);
  assert.equal(new Set(allOpen.rows.map((row) => row.key)).size, allOpen.rows.length);
  assert.deepEqual(
    allOpen.rows.filter((row) => row.item.type === 'message').map((row) => row.key),
    ['user', 'progress', 'answer'],
  );
});

test('deliverables remain mounted outside folded work', () => {
  const input = longTurn(3);
  const tool = input.find((item) => item.type === 'toolExecution' && item.execution.toolCallId === 'tool-2');
  tool.execution.toolCall.name = 'send_file_to_user';
  const { rows } = project(input);
  assert.equal(rows.flatMap((row) => row.deliverables).length, 1);
  assert.equal(rows.flatMap((row) => row.deliverables)[0].toolCallId, 'tool-2');
  assert.equal(rows.at(-1).item.message.id, 'answer');
});

test('prepending complete turns does not change existing row keys or lose expanded state', () => {
  const input = longTurn();
  const before = project(input);
  const header = before.rows.find((row) => row.isTurnAnchor);
  const expanded = { [header.turnKey]: true };
  const openBefore = project(input, expanded);
  const after = project(
    [message('older-user', 'user', -20), message('older-answer', 'assistant', -10), ...input],
    expanded,
  );
  const oldKeys = new Set(openBefore.rows.map((row) => row.key));
  assert.deepEqual(
    after.rows.filter((row) => oldKeys.has(row.key)).map((row) => row.key),
    [...oldKeys],
  );
  assert.equal(after.rows.find((row) => row.key === header.key).turnOpen, true);
});

test('live work and newly appended messages remain visible', () => {
  const input = longTurn(2);
  const items = buildRenderItems(input, false, true);
  const meta = buildTurnWorkMeta(items, true);
  const rows = projectTimelineItems(items, meta, buildTurnFoldAnchorKeys(items, meta), new Map(), {}, {});
  assert.equal(rows.filter((row) => row.item.type === 'reasoning').length, 2);
  assert.ok(rows.every((row) => row.contentOpen));
  assert.equal(rows.at(-1).item.message.id, 'answer');
});

test('prepending the start of a partially loaded turn preserves its folded header and final answer', () => {
  const input = longTurn();
  const tail = project(input.slice(100));
  const header = tail.rows.find((row) => row.isTurnAnchor);
  const expanded = { [header.turnKey]: true };
  const full = project(input, expanded);
  assert.equal(full.rows.find((row) => row.isTurnAnchor).key, header.key);
  assert.equal(full.rows.find((row) => row.isTurnAnchor).turnOpen, true);
  assert.equal(full.rows.at(-1).key, tail.rows.at(-1).key);
  assert.equal(full.rows.at(-1).item.message.id, 'answer');
});
