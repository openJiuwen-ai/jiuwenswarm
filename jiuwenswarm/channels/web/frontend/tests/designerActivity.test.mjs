import assert from 'node:assert/strict';
import test from 'node:test';

import {
  designerActivityItems,
  designerActivityLines,
  designerActivityText,
  isDesignerLeaderNodeId,
} from '../node_modules/.cache/designer-activity/designerActivity.js';

test('leader node id is virtual', () => {
  assert.equal(isDesignerLeaderNodeId('__leader__'), true);
  assert.equal(isDesignerLeaderNodeId('n_brief'), false);
});

test('activity lines prefer tail then latest', () => {
  assert.deepEqual(
    designerActivityLines({
      activity: { kind: 'tool_call', text: 'patch', tool: 'designer_graph_patch' },
      activity_tail: ['reading brief', 'building graph'],
    }),
    ['reading brief', 'building graph'],
  );
  assert.equal(
    designerActivityText({ kind: 'tool_call', text: 'calling patch', tool: 'designer_graph_patch' }),
    'designer_graph_patch · calling patch',
  );
});

test('activity items preserve node-agent thinking and tool-call kinds', () => {
  const state = {
    activity: { kind: 'tool_call', text: 'calling call_video_model', tool: 'call_video_model', at: 3 },
    activity_tail: ['planning clip', 'call_video_model · calling call_video_model'],
    activity_log: [
      { kind: 'thinking', text: 'planning clip', at: 1 },
      { kind: 'stage', text: 'preparing references', at: 2 },
      { kind: 'tool_call', text: 'calling call_video_model', tool: 'call_video_model', at: 3 },
    ],
  };

  assert.deepEqual(designerActivityItems(state), [
    { kind: 'thinking', text: 'planning clip', tool: '', at: 1 },
    { kind: 'stage', text: 'preparing references', tool: '', at: 2 },
    { kind: 'tool_call', text: 'calling call_video_model', tool: 'call_video_model', at: 3 },
  ]);
  assert.deepEqual(designerActivityLines(state), [
    'planning clip',
    'preparing references',
    'call_video_model · calling call_video_model',
  ]);
});
