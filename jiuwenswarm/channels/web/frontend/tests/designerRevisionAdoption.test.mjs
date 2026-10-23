import assert from 'node:assert/strict';
import test from 'node:test';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window, document: dom.window.document,
  localStorage: dom.window.localStorage, sessionStorage: dom.window.sessionStorage,
  HTMLElement: dom.window.HTMLElement, Element: dom.window.Element,
  ResizeObserver: class { observe() {} unobserve() {} disconnect() {} },
  requestAnimationFrame: (callback) => setTimeout(callback, 0),
  cancelAnimationFrame: clearTimeout, IS_REACT_ACT_ENVIRONMENT: true,
});
window.ResizeObserver = globalThis.ResizeObserver;
window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });

const base = '../node_modules/.cache/designer-revision-adoption/';
const { DesignerPage } = await import(`${base}features/designer/components/DesignerPage.js`);
const { useDesignerStore } = await import(`${base}features/designer/designerStore.js`);
const { useDesignerRunStore } = await import(`${base}features/designer/designerRunStore.js`);
const { useDesignerUiStore } = await import(`${base}features/designer/designerUiStore.js`);
const { webClient } = await import(`${base}services/webClient.js`);

test('the same node adopts both successive generated candidates in one mounted page', async (t) => {
  const ref = (name) => ({ kind: 'image', uri: `file:///tmp/${name}.png`, mime_type: 'image/png' });
  const graph = {
    graph_id: 'g_revisions', project_id: 'p_revisions', title: 'Repeated generation',
    nodes: [{ id: 'n_image', type: 'image', label: 'Image', config: { role: 'image' }, layout: { x: 0, y: 0 } }],
    edges: [], metadata: {},
  };
  const calls = [];
  t.mock.method(webClient, 'request', async (method, params) => {
    if (method === 'designer.run.choose_output') {
      calls.push(params);
      const run = structuredClone(useDesignerRunStore.getState().run);
      const state = run.node_states.n_image;
      state.output_ref = state.candidate_output_ref;
      state.candidate_output_ref = null;
      return { run };
    }
    if (method === 'designer.graph.get') return { graph };
    return {};
  });
  useDesignerStore.setState({ domainGraph: graph, graphId: 'loading_graph', loadStatus: 'loading', bootstrapInProgress: false });
  useDesignerUiStore.setState({ chatSidebarOpen: false, assetsSidebarOpen: false });
  const makeRun = (version, original, incoming) => ({
    run_id: 'run_lifelong', graph_id: graph.graph_id, status: 'completed',
    node_states: { n_image: { status: 'completed', version, completed_at: version,
      output_ref: ref(original), candidate_output_ref: ref(incoming) } },
  });
  useDesignerRunStore.getState().applyRun(makeRun(1, 'old', 'first'));
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => { root.render(React.createElement(DesignerPage)); });
    assert.equal(calls.length, 1);
    await act(async () => { useDesignerRunStore.getState().applyRun(makeRun(2, 'first', 'second')); });
    assert.equal(calls.length, 2);
    assert.equal(useDesignerRunStore.getState().run.node_states.n_image.output_ref.uri, ref('second').uri);
  } finally {
    await act(async () => root.unmount());
    useDesignerRunStore.getState().applyRun(null);
  }
});
