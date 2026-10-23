import assert from 'node:assert/strict';
import test from 'node:test';

import { webClient } from '../node_modules/.cache/designer-run-state/services/webClient.js';
import { useDesignerStore } from '../node_modules/.cache/designer-run-state/features/designer/designerStore.js';
import { useDesignerRunStore } from '../node_modules/.cache/designer-run-state/features/designer/designerRunStore.js';

const graph = {
  graph_id: 'g_rerun', metadata: { session_id: 'design_real' },
  nodes: [{ id: 'n_image', type: 'image', config: {} }], edges: [],
};
const completed = { run_id: 'run_same', graph_id: graph.graph_id, status: 'completed', node_states: {} };

for (const action of ['rerunNode', 'advance', 'restart']) {
  test(`${action} sends the owning session and immediately follows the running snapshot`, async (t) => {
    const requests = [];
    t.mock.method(webClient, 'request', async (method, params) => {
      requests.push({ method, params });
      return method === 'designer.graph.get' ? { graph } : { run: { ...completed, status: 'running' } };
    });
    useDesignerStore.setState({ domainGraph: graph, flushSave: async () => {} });
    useDesignerRunStore.getState().applyRun(completed);
    try {
      await useDesignerRunStore.getState()[action](graph, 'n_image');
      const start = requests.find((item) => item.method === 'designer.run.start');
      assert.equal(start.params.session_id, 'design_real');
      assert.equal(useDesignerRunStore.getState().isRunning, true);
    } finally {
      useDesignerRunStore.getState().applyRun(null);
    }
  });
}

for (const status of ['completed', 'failed']) {
  test(`polling observes ${status} without a push or refresh`, async (t) => {
    const requests = [];
    t.mock.method(webClient, 'request', async (method) => {
      requests.push(method);
      if (method === 'designer.graph.get') return { graph };
      return { run: { ...completed, status } };
    });
    useDesignerStore.setState({ domainGraph: graph });
    useDesignerRunStore.getState().applyRun({ ...completed, status: 'running' });
    try {
      await new Promise((resolve) => setTimeout(resolve, 500));
      assert.ok(requests.includes('designer.run.get'));
      assert.equal(useDesignerRunStore.getState().run.status, status);
      assert.equal(useDesignerRunStore.getState().isRunning, false);
    } finally {
      useDesignerRunStore.getState().applyRun(null);
    }
  });
}
