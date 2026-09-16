import assert from 'node:assert/strict';
import test from 'node:test';

import { mergeReviewerProgress, mergeToolResultProgress, shouldDropToolResult } from '../node_modules/.cache/tool-result-lifecycle/stores/toolResultLifecycle.js';

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

test('trusted terminal reviewer metadata survives late pending and metadata-free updates', () => {
  const terminal = result({
    result: 'permission denied',
    success: false,
    reviewer: {
      decision_source: 'manual_approval',
      final_reviewer_status: 'denied',
    },
  });

  assert.deepEqual(
    mergeToolResultProgress(terminal, result({ reviewer: { reviewer_status: 'manual' } })),
    { ...result({ reviewer: { reviewer_status: 'manual' } }), success: false, reviewer: terminal.reviewer },
  );
  assert.deepEqual(
    mergeToolResultProgress(terminal, result()),
    { ...result(), success: false, reviewer: terminal.reviewer },
  );
});

test('trusted terminal reviewer progress survives a late nonterminal update', () => {
  const terminal = {
    decision_source: 'manual_approval',
    final_reviewer_status: 'denied',
  };

  assert.equal(
    mergeReviewerProgress(terminal, { reviewer_status: 'manual' }),
    terminal,
  );
});

test('the first trusted terminal reviewer progress wins conflicting late updates', () => {
  const terminal = {
    decision_source: 'manual_approval',
    final_reviewer_status: 'denied',
  };

  assert.equal(
    mergeReviewerProgress(terminal, {
      decision_source: 'auto_reviewer',
      final_reviewer_status: 'approved',
    }),
    terminal,
  );
});

test('a newer trusted terminal replaces pending metadata', () => {
  const pending = result({ reviewer: { reviewer_status: 'manual' } });
  const terminal = result({
    success: false,
    reviewer: {
      decision_source: 'auto_reviewer',
      final_reviewer_status: 'denied',
    },
  });

  assert.equal(mergeToolResultProgress(pending, terminal), terminal);
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

test('timeout is distinct from an error with identical content', () => {
  const errorResult = result({ success: false });
  const timeoutResult = result({ success: false, timedOut: true });

  assert.equal(shouldDropToolResult('error', errorResult, timeoutResult), false);
  assert.equal(shouldDropToolResult('timeout', timeoutResult, timeoutResult), true);
});

test('late pending result cannot replace a terminal execution', () => {
  const pendingResult = result({ pending: true });

  assert.equal(shouldDropToolResult('completed', result(), pendingResult), true);
  assert.equal(shouldDropToolResult('error', result({ success: false }), pendingResult), true);
  assert.equal(shouldDropToolResult('timeout', result({ success: false, timedOut: true }), pendingResult), true);
});

test('reviewer decision changes participate in duplicate detection', () => {
  const first = result({ reviewer: { reviewer_status: 'in_progress' } });
  const second = result({ reviewer: { reviewer_status: 'approved' } });

  assert.equal(shouldDropToolResult('completed', first, second), false);
});

test('later result without reviewer preserves the existing reviewer', () => {
  const reviewer = { reviewer_status: 'approved' };

  assert.deepEqual(
    mergeToolResultProgress(result({ reviewer }), result()),
    result({ reviewer }),
  );
});

test('reviewer and beam progress are inherited together', () => {
  const reviewer = { reviewer_status: 'approved' };
  const beamSearch = { roundIndex: 3, graph: { nodes: [], edges: [] } };

  assert.deepEqual(
    mergeToolResultProgress(result({ reviewer, beamSearch }), result()),
    result({ reviewer, beamSearch }),
  );
});

test('incoming reviewer replaces the existing reviewer', () => {
  const existing = result({ reviewer: { reviewer_status: 'in_progress' } });
  const incoming = result({ reviewer: { reviewer_status: 'approved' } });

  assert.equal(mergeToolResultProgress(existing, incoming), incoming);
});

test('merged reviewer result can be dropped as an exact duplicate', () => {
  const existing = result({ reviewer: { reviewer_status: 'approved' } });
  const merged = mergeToolResultProgress(existing, result());

  assert.equal(shouldDropToolResult('completed', existing, merged), true);
});
