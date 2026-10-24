import assert from 'node:assert/strict';
import test from 'node:test';

import {
  designerGraphClient,
  useDesignerStore,
} from '../node_modules/.cache/designer-store-save/designerStore.mjs';

const graph = (prompt) => ({
  graph_id: 'g_save',
  nodes: [{ id: 'n_character', type: 'image', label: 'Barista', config: { prompt } }],
  edges: [],
  metadata: {},
});

const promptOf = (state) => state.domainGraph.nodes[0].config.prompt;

function deferredSaves() {
  const calls = [];
  designerGraphClient.save = (sent) =>
    new Promise((resolve) => {
      calls.push({ sent, resolve: () => resolve({ graph: sent }) });
    });
  return calls;
}

const settle = (ms = 0) => new Promise((resolve) => setTimeout(resolve, ms));

const SAVE_DEBOUNCE_MS = 500;

test('a save waits for the one in flight and sends the latest graph', async () => {
  const calls = deferredSaves();
  const store = useDesignerStore.getState();
  store.applyGraph(graph('short hair'));

  const first = store.flushSave();
  store.updateNodeConfig('n_character', (config) => ({ ...config, prompt: 'long hair' }));
  const second = store.flushSave();
  await settle();

  assert.equal(calls.length, 1);
  calls[0].resolve();
  await first;
  await settle();

  assert.equal(calls.length, 2);
  assert.equal(calls[1].sent.nodes[0].config.prompt, 'long hair');
  calls[1].resolve();
  await second;
  assert.equal(promptOf(useDesignerStore.getState()), 'long hair');
});

test('an older save response does not undo an edit made while it was in flight', async () => {
  const calls = deferredSaves();
  const store = useDesignerStore.getState();
  store.applyGraph(graph('short hair'));

  const first = store.flushSave();
  store.updateNodeConfig('n_character', (config) => ({ ...config, prompt: 'long hair' }));
  calls[0].resolve();
  await first;

  assert.equal(promptOf(useDesignerStore.getState()), 'long hair');

  await settle(SAVE_DEBOUNCE_MS + 100);
  assert.equal(calls.length, 2);
  assert.equal(calls[1].sent.nodes[0].config.prompt, 'long hair');
  calls[1].resolve();
  await settle();
  assert.equal(useDesignerStore.getState().saveStatus, 'saved');
  store.reset();
});
