import assert from 'node:assert/strict';
import { readdir, readFile } from 'node:fs/promises';
import test from 'node:test';

import { createTrajectoryV2Reducer } from '../node_modules/.cache/trajectory-projector/projector.mjs';

// Replay vectors shared with agent-core
// (tests/unit_tests/agent_evolving/trajectory/fixtures/v2). The repositories
// share no root, so both hold a copy; a change to a v2 payload or to replay
// semantics updates both.
const VECTOR_DIR = new URL('./fixtures/trajectory-v2/', import.meta.url);

// Diagnostics only the viewer produces; agent-core's replay does not state them.
const VIEW_ONLY_CODES = new Set(['v2.checkpoint_recovery', 'v2.partial_window']);

const vectorNames = (await readdir(VECTOR_DIR)).filter(name => name.endsWith('.json')).sort();

for (const name of vectorNames) {
  test(`v2 replay vector: ${name}`, async () => {
    const vector = JSON.parse(await readFile(new URL(name, VECTOR_DIR), 'utf8'));
    const records = vector.records.map(span => ({
      resourceSpans: [{ scopeSpans: [{ spans: [span] }] }],
    }));
    const reduction = createTrajectoryV2Reducer().apply(records);

    const codes = [...reduction.diagnostics, ...[...reduction.subjects.values()].flatMap(s => s.diagnostics)]
      .map(diagnostic => diagnostic.code)
      .filter(code => !VIEW_ONLY_CODES.has(code));
    assert.deepEqual([...new Set(codes)].sort(), [...new Set(vector.expected.issue_codes)].sort());

    const handled = new Set([...reduction.subjects.values()].flatMap(s => [...s.handledInferenceIds]));
    for (const inferenceId of Object.keys(vector.expected.by_inference)) {
      assert.ok(handled.has(inferenceId), `${inferenceId} should be read from a replayed window`);
    }
    for (const subjectId of Object.keys(vector.expected.windows)) {
      if (Object.keys(vector.expected.windows[subjectId]).length === 0) continue;
      assert.ok(reduction.subjects.has(subjectId), `subject ${subjectId} should be replayed`);
    }
  });
}
