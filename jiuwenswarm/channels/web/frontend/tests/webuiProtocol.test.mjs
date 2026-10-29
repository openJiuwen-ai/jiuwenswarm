import test from 'node:test';
import assert from 'node:assert/strict';

import {
  DEFAULT_WEBUI_LIMITS,
  createProtocolPipeline,
  processRenderFrame,
} from '../node_modules/.cache/webui-protocol/protocol/index.js';
import { createCatalog } from '../node_modules/.cache/webui-protocol/catalog/index.js';
import {
  DEMO_COMPONENT,
  DEMO_SLOT,
  demoCatalog,
} from '../node_modules/.cache/webui-protocol/testing/demoCatalog.js';
import {
  demoClearFrame,
  demoErrorFrame,
  demoRenderFrame,
  demoTree,
} from '../node_modules/.cache/webui-protocol/testing/fixtures.js';

const protocol = createProtocolPipeline(demoCatalog);

test('render frame is validated, defaulted and keyed by path', () => {
  const result = protocol.processRenderFrame(demoRenderFrame());
  assert.equal(result.ok, true);
  const frame = result.value;
  assert.equal(frame.kind, 'render');
  assert.deepEqual(frame.target, {
    sessionId: 'sess-1',
    slotId: DEMO_SLOT,
    packageId: 'pkg.demo',
    instanceKey: 'main',
  });
  assert.equal(frame.revision, 1);
  assert.equal(frame.schemaVersion, 1);
  assert.equal(frame.nodeCount, 2);
  assert.equal(frame.tree.key, '0');
  assert.equal(frame.tree.props.title, 'hello');
  assert.equal(frame.tree.props.tone, 'neutral');
  assert.equal(frame.tree.props.active, false);
  assert.equal(frame.tree.children[0].key, '0.0');
  assert.equal(frame.tree.children[0].action.id, 'demo.refresh');
  assert.deepEqual(frame.tree.children[0].action.args, { page: 1 });
});

test('protocol does not mutate the incoming payload', () => {
  const input = demoRenderFrame();
  const before = JSON.stringify(input);
  const result = protocol.processRenderFrame(input);
  assert.equal(result.ok, true);
  assert.equal(JSON.stringify(input), before);
});

test('unknown component rejects the whole tree', () => {
  const result = protocol.processRenderFrame(demoRenderFrame({ root: { component: 'nope', props: {} } }));
  assert.equal(result.ok, false);
  assert.equal(result.failure.kind, 'reject');
  assert.equal(result.failure.code, 'UNKNOWN_COMPONENT');
});

test('unknown props and invalid tokens are rejected', () => {
  const unknownProp = protocol.processRenderFrame(demoRenderFrame({ root: { component: DEMO_COMPONENT, props: { nope: 1 } } }));
  assert.equal(unknownProp.ok, false);
  assert.equal(unknownProp.failure.code, 'SCHEMA_ERROR');

  const badToken = protocol.processRenderFrame(
    demoRenderFrame({ root: { component: DEMO_COMPONENT, props: { tone: 'purple' } } }),
  );
  assert.equal(badToken.ok, false);
  assert.equal(badToken.failure.code, 'SCHEMA_ERROR');
});

test('children and actions follow the component rule', () => {
  const leafCatalog = createCatalog({
    components: [{ name: 'leaf', props: {}, allowChildren: false, allowAction: false }],
    slots: [{ id: DEMO_SLOT, display: 'list' }],
  });
  const childrenOnLeaf = processRenderFrame(
    demoRenderFrame({ root: { component: 'leaf', props: {}, children: [{ component: 'leaf', props: {} }] } }),
    leafCatalog,
  );
  assert.equal(childrenOnLeaf.ok, false);
  assert.equal(childrenOnLeaf.failure.code, 'SCHEMA_ERROR');

  const actionOnLeaf = processRenderFrame(
    demoRenderFrame({ root: { component: 'leaf', props: {}, action: { id: 'leaf.run' } } }),
    leafCatalog,
  );
  assert.equal(actionOnLeaf.ok, false);
  assert.equal(actionOnLeaf.failure.code, 'SCHEMA_ERROR');
});

test('tree depth and node count limits are enforced', () => {
  let root = { component: DEMO_COMPONENT, props: {} };
  for (let depth = 1; depth < 9; depth += 1) {
    root = { component: DEMO_COMPONENT, props: {}, children: [root] };
  }
  const tooDeep = protocol.processRenderFrame(demoRenderFrame({ root }));
  assert.equal(tooDeep.ok, false);
  assert.equal(tooDeep.failure.code, 'SCHEMA_ERROR');

  const smallCatalog = createProtocolPipeline(demoCatalog, { ...DEFAULT_WEBUI_LIMITS, maxNodes: 1 });
  const tooMany = smallCatalog.processRenderFrame(demoRenderFrame());
  assert.equal(tooMany.ok, false);
  assert.equal(tooMany.failure.code, 'SCHEMA_ERROR');
});

test('frame size limit rejects instead of truncating', () => {
  const tinyBudget = createProtocolPipeline(demoCatalog, { ...DEFAULT_WEBUI_LIMITS, maxFrameBytes: 16 });
  const result = tinyBudget.processRenderFrame(demoRenderFrame());
  assert.equal(result.ok, false);
  assert.equal(result.failure.code, 'FRAME_TOO_LARGE');
});

test('unsupported schema version and mode are distinguished from schema errors', () => {
  const version = protocol.processRenderFrame(demoRenderFrame({ schemaVersion: 2 }));
  assert.equal(version.ok, false);
  assert.equal(version.failure.kind, 'unsupported');
  assert.equal(version.failure.code, 'UNSUPPORTED_SCHEMA_VERSION');

  const mode = protocol.processRenderFrame(demoRenderFrame({ mode: 'append' }));
  assert.equal(mode.ok, false);
  assert.equal(mode.failure.kind, 'unsupported');
  assert.equal(mode.failure.code, 'UNSUPPORTED_MODE');
});

test('malformed frames are rejected with stable codes', () => {
  const cases = [
    null,
    {},
    demoRenderFrame({ target: { session_id: '' } }),
    demoRenderFrame({ target: { slot_id: 'application.menu' } }),
    demoRenderFrame({ revision: '1' }),
    demoRenderFrame({ root: null }),
    demoRenderFrame({ root: { component: DEMO_COMPONENT, props: {}, action: { id: '' } } }),
  ];
  for (const input of cases) {
    const result = protocol.processRenderFrame(input);
    assert.equal(result.ok, false);
    assert.equal(result.failure.kind, 'reject');
    assert.equal(result.failure.code, 'SCHEMA_ERROR');
  }
});

test('action args keep their wire shape and size limit', () => {
  const withArgs = protocol.processRenderFrame(
    demoRenderFrame({
      root: { component: DEMO_COMPONENT, props: {}, action: { id: 'demo.run', args: { page: 2, nested: [1, 2] } } },
    }),
  );
  assert.equal(withArgs.ok, true);
  assert.deepEqual(withArgs.value.tree.action.args, { page: 2, nested: [1, 2] });

  const oversized = protocol.processRenderFrame(
    demoRenderFrame({
      root: { component: DEMO_COMPONENT, props: {}, action: { id: 'demo.run', args: { text: 'x'.repeat(9000) } } },
    }),
  );
  assert.equal(oversized.ok, false);
  assert.equal(oversized.failure.code, 'SCHEMA_ERROR');
});

test('clear frame is normalized and requires a valid revision', () => {
  const clear = protocol.processClearFrame(demoClearFrame({ revision: 3, reason: 'session_closed' }));
  assert.equal(clear.ok, true);
  assert.equal(clear.value.kind, 'clear');
  assert.equal(clear.value.revision, 3);
  assert.equal(clear.value.reason, 'session_closed');

  const bad = protocol.processClearFrame(demoClearFrame({ revision: -1 }));
  assert.equal(bad.ok, false);
  assert.equal(bad.failure.code, 'SCHEMA_ERROR');
});

test('error frame is normalized and requires a code', () => {
  const error = protocol.processErrorFrame(demoErrorFrame({ revision: 0, code: 'SCHEMA_ERROR', message: 'bad tree' }));
  assert.equal(error.ok, true);
  assert.equal(error.value.kind, 'error');
  assert.equal(error.value.code, 'SCHEMA_ERROR');
  assert.equal(error.value.message, 'bad tree');

  const missingCode = protocol.processErrorFrame({ target: demoRenderFrame().target, revision: 0 });
  assert.equal(missingCode.ok, false);
  assert.equal(missingCode.failure.code, 'SCHEMA_ERROR');
});

test('same demo tree can be rebuilt and keeps deterministic keys', () => {
  const first = protocol.processRenderFrame(demoRenderFrame());
  const second = protocol.processRenderFrame(demoRenderFrame());
  assert.equal(first.ok, true);
  assert.equal(second.ok, true);
  assert.equal(first.value.tree.key, second.value.tree.key);
  assert.equal(first.value.tree.children[0].key, second.value.tree.children[0].key);
  assert.deepEqual(first.value.tree, second.value.tree);
  assert.notEqual(first.value.tree, second.value.tree);

  const custom = { component: DEMO_COMPONENT, props: { title: 'custom' }, children: [demoTree()] };
  const built = protocol.processRenderFrame(demoRenderFrame({ root: custom }));
  assert.equal(built.ok, true);
  assert.equal(built.value.tree.children[0].key, '0.0');
  assert.equal(built.value.tree.children[0].children[0].key, '0.0.0');
});
