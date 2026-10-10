import assert from 'node:assert/strict';
import test from 'node:test';

import { mergeToolResultProgress, shouldDropToolResult } from '../node_modules/.cache/tool-result-lifecycle/stores/toolResultLifecycle.js';

const result = (overrides = {}) => ({
  toolName: 'example_tool',
  toolCallId: 'call-1',
  result: 'done',
  success: true,
  ...overrides,
});

test('ordinary tool result is returned unchanged', () => {
  const incoming = result();

  assert.equal(mergeToolResultProgress(result({ result: '' }), incoming), incoming);
});

test('final result inherits the last streamed beam graph when omitted', () => {
  const beamSearch = { roundIndex: 2, graph: { nodes: [], edges: [] } };
  const incoming = result({ toolName: 'symphony_compose_graph' });

  assert.deepEqual(mergeToolResultProgress(result({ beamSearch }), incoming), { ...incoming, beamSearch });
});

test('final beam graph replaces the streamed graph', () => {
  const streamed = { roundIndex: 1, graph: { nodes: [], edges: [] } };
  const final = { roundIndex: 2, graph: { nodes: [], edges: [] } };
  const incoming = result({ beamSearch: final });

  assert.equal(mergeToolResultProgress(result({ beamSearch: streamed }), incoming), incoming);
});

test('pending execution is not dropped when identical final result arrives', () => {
  const finalResult = result();

  assert.equal(shouldDropToolResult('pending', finalResult, finalResult), false);
  assert.equal(shouldDropToolResult('completed', finalResult, finalResult), true);
});

test('pending execution is not dropped when an error result arrives', () => {
  const errorResult = result({ success: false });

  assert.equal(shouldDropToolResult('pending', errorResult, errorResult), false);
  assert.equal(shouldDropToolResult('error', errorResult, errorResult), true);
});

test('beam graph participates in duplicate detection', () => {
  const first = result({ beamSearch: { roundIndex: 1, graph: { nodes: [], edges: [] } } });
  const second = result({ beamSearch: { roundIndex: 2, graph: { nodes: [], edges: [] } } });

  assert.equal(shouldDropToolResult('completed', first, second), false);
});
