import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { act, createElement, createRef } from 'react';
import { createRoot } from 'react-dom/client';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { SkillGraphTab, useRetrievalIndexBuild, webClient } from '../node_modules/.cache/skill-graph-panel/panel.mjs';

const select = (container, id) => container.querySelector(`[data-testid="${id}"]`);
const retrievalStatus = {
  enabled: false, index_enabled: false, candidate_scale: 'small',
  estimated_candidate_tokens: 0, candidate_budget_tokens: 1024, effective_strategy: 'legacy',
  index_recommended: false, layout: 'flat', index_state: 'missing', build_supported: false,
  build_status: 'disabled', build_progress: 0, build_message: '', build_error: '', build_id: '',
  index_exists: false, indexed_count: 0, searchable_count: 0, profile_scope: 'default',
  profile_session_id: '', profile_model_name: '', pinned_index_revision: '', profile_recovery_error: '',
};

async function setup(t, request) {
  const dom = new JSDOM('<main></main>', { url: 'http://localhost/', pretendToBeVisual: true });
  const { window } = dom;
  class ResizeObserver {
    observe() {}
    disconnect() {}
  }
  for (const [name, value] of Object.entries({
    window, document: window.document, IS_REACT_ACT_ENVIRONMENT: true, ResizeObserver,
  })) {
    const previous = Object.getOwnPropertyDescriptor(globalThis, name);
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
    t.after(() => {
      if (previous) Object.defineProperty(globalThis, name, previous);
      else delete globalThis[name];
    });
  }
  t.mock.method(window.HTMLCanvasElement.prototype, 'getContext', () => null);
  t.mock.method(webClient, 'request', request);

  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: {} } } });
  const container = window.document.querySelector('main');
  const root = createRoot(container);
  const messages = [];
  const graphRef = createRef();
  function View({ enabled }) {
    const { startRetrievalIndexBuild } = useRetrievalIndexBuild({
      isConnected: true,
      withSession: (params = {}) => params,
      showMessage: (variant, message) => messages.push([variant, message]),
    });
    return createElement(SkillGraphTab, {
      isActive: true,
      isConnected: true,
      symphonyEnabled: enabled,
      symphonyEnabledDraft: enabled,
      symphonySaving: false,
      skillGraphPanelRef: graphRef,
      onStartRetrievalIndexBuild: startRetrievalIndexBuild,
    });
  }
  return {
    container,
    messages,
    getPanel: () => graphRef.current,
    render: (enabled) => act(async () => {
      root.render(createElement(I18nextProvider, { i18n }, createElement(View, { enabled })));
    }),
    dispose: async () => {
      await act(async () => root.unmount());
      window.close();
    },
  };
}

test('opening the graph with orchestration disabled is quiet and keeps the build controls unavailable', async (t) => {
  const calls = [];
  const app = await setup(t, async (method) => {
    calls.push(method);
    if (method === 'skills.graph.get') throw new Error('Symphony orchestration is disabled');
    return { success: true, enabled: false };
  });
  try {
    await app.render(false);
    assert.deepEqual(calls, []);
    assert.equal(select(app.container, 'skill-graph-panel-error'), null);
    assert.equal(select(app.container, 'skill-graph-panel-action-incremental-build').disabled, true);
    assert.equal(app.getPanel().refresh(), false);
  } finally {
    await app.dispose();
  }
});

test('a graph build can start when enabling orchestration with retrieval disabled', async (t) => {
  const calls = [];
  const app = await setup(t, async (method) => {
    calls.push(method);
    if (method === 'skills.retrieval.status') {
      return retrievalStatus;
    }
    return { success: true, build_progress: { status: 'running' } };
  });
  try {
    await app.render(false);
    await act(async () => app.getPanel().startIncrementalBuild());
    await app.render(true);
    assert.equal(calls.filter((method) => method === 'skills.graph.build').length, 1);
    assert.ok(calls.includes('skills.graph.status'));
    assert.ok(calls.includes('skills.retrieval.status'));
    assert.ok(!calls.includes('skills.retrieval.index_build'));
    assert.deepEqual(app.messages, []);
  } finally {
    await app.dispose();
  }
});

test('a supported retrieval build starts when the server did not recommend an index', async (t) => {
  const indexBuildRequests = [];
  const app = await setup(t, async (method, params) => {
    if (method === 'skills.retrieval.status') {
      return {
        ...retrievalStatus,
        enabled: true, candidate_scale: 'large', effective_strategy: 'large_flat',
        build_supported: true, build_status: 'idle',
      };
    }
    if (method === 'skills.retrieval.index_build') {
      indexBuildRequests.push(params);
      return { success: true, build_id: 'build-1' };
    }
    return { success: true, build_progress: { status: 'running' } };
  });
  try {
    await app.render(true);
    await act(async () => app.getPanel().startIncrementalBuild());
    assert.deepEqual(indexBuildRequests, [{ force: false, source: 'web' }]);
    assert.deepEqual(app.messages, [['success', 'skills.retrieval.buildStarted']]);
  } finally {
    await app.dispose();
  }
});

test('a real graph read failure remains visible when orchestration is enabled', async (t) => {
  const app = await setup(t, async (method) => {
    if (method === 'skills.graph.get') throw new Error('graph read failed');
    return { success: true };
  });
  try {
    await app.render(true);
    assert.equal(select(app.container, 'skill-graph-panel-error-text').textContent, 'graph read failed');
  } finally {
    await app.dispose();
  }
});
