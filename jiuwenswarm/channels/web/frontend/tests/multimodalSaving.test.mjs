import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { JSDOM } from 'jsdom';

const dom = new JSDOM('<!doctype html><body><div id="root"></div></body>', {
  url: 'http://localhost', pretendToBeVisual: true,
});
for (const name of ['window', 'document', 'navigator', 'Node', 'HTMLElement', 'localStorage', 'CustomEvent']) {
  Object.defineProperty(globalThis, name, { configurable: true, value: dom.window[name] });
}
globalThis.IS_REACT_ACT_ENVIRONMENT = true;
const { act, createElement } = await import('react');
const { createRoot } = await import('react-dom/client');
await build({
  stdin: { contents: `
    export { default as i18n } from './src/i18n';
    export { SettingsServicesProvider } from './src/features/settings/services/SettingsServicesProvider';
    export { SettingItemRenderer } from './src/features/settings/components/SettingItemRenderer';
    export { MultimodalSettingsFeedback } from './src/features/settings/modules/models/MultimodalSettingsFeedback';
    export { multimodalToolItems } from './src/features/settings/modules/agent/MultimodalToolSettings';
  `, resolveDir: process.cwd(), loader: 'tsx' },
  outfile: 'node_modules/.cache/multimodal-saving.mjs', bundle: true, format: 'esm', platform: 'node',
  packages: 'external', loader: { '.css': 'empty', '.svg': 'text', '.png': 'dataurl' },
  define: { 'import.meta.env.DEV': 'false', 'import.meta.env.PROD': 'false', 'import.meta.glob': 'testAssetGlob' },
  banner: { js: 'const testAssetGlob = () => ({});' },
});
const { SettingsServicesProvider, SettingItemRenderer, multimodalToolItems, MultimodalSettingsFeedback, i18n } = await import('../node_modules/.cache/multimodal-saving.mjs');
const tools = ['vision', 'audio', 'video', 'visual_gen', 'video_gen'];

async function mount({ failRead = () => false, models } = {}) {
  const root = createRoot(document.getElementById('root'));
  const writes = [];
  const snapshot = {
    schema_version: 1,
    models: models ?? ['a', 'b'].map((id) => ({ model_id: id, display_name: id, capabilities: tools })),
    tools: Object.fromEntries(tools.map((tool) => [tool, { model_id: 'a', enabled: true }])),
  };
  const request = async (method, params) => {
    if (method === 'multimodal.models.list') {
      if (failRead()) throw new Error('temporary read failure');
      return structuredClone(snapshot);
    }
    return new Promise((resolve, reject) => writes.push({ params, resolve, reject }));
  };
  const render = async (isConnected = true) => act(async () => root.render(createElement(SettingsServicesProvider, {
    isConnected, connectionState: isConnected ? 'ready' : 'disconnected', request,
  }, [
    createElement(MultimodalSettingsFeedback, { key: 'feedback' }),
    ...tools.flatMap(multimodalToolItems).map((item) => createElement(SettingItemRenderer, { key: item.id, item })),
  ])));
  await render();
  return {
    render,
    writes,
    switch: (tool) => document.querySelector(`[data-testid="settings-switch"][data-variant="${tool}-enabled"]`),
    select: (tool) => document.querySelector(`[data-testid="settings-select"][data-variant="${tool}-model"]`),
    finish: async (index, error) => act(async () => {
      const write = writes[index];
      if (error) write.reject(error);
      else {
        const { tool_id, ...updates } = write.params;
        Object.assign(snapshot.tools[tool_id], updates);
        write.resolve({ saved: true, changed: true, applied_without_restart: true, snapshot: structuredClone(snapshot) });
      }
    }),
    close: async () => act(async () => root.unmount()),
  };
}

test('duplicate model labels show only display names, expose option details, and save the chosen ID', async () => {
  const models = ['a', 'b'].map((model_id) => ({
    model_id, display_name: 'Shared model', model_name: 'qwen3.7-max',
    api_base: 'https://models.example.com/compatible-mode/v1', capabilities: tools,
  }));
  const details = models.map((model) => `qwen3.7-max\n${model.api_base}\n${model.model_id}`);
  const ui = await mount({ models });
  try {
    const trigger = ui.select('vision');
    const selectedLabel = trigger.querySelector('[data-testid="settings-select-label"]');
    assert.equal(selectedLabel.textContent.trim(), 'Shared model');
    assert.equal(trigger.title, 'Shared model');
    await act(async () => trigger.click());
    for (const [index, title] of details.entries()) {
      const option = document.querySelector(`[role="option"][data-variant="${index}"]`);
      assert.equal(option.textContent.trim(), 'Shared model');
      assert.equal(option.title, title);
    }
    await act(async () => document.querySelector('[role="option"][data-variant="1"]').click());
    assert.deepEqual(ui.writes.map((write) => write.params), [{ tool_id: 'vision', model_id: 'b' }]);
    await ui.finish(0);
    assert.equal(trigger.textContent.trim(), 'Shared model');
    await act(async () => trigger.click());
    assert.equal(document.querySelector('[role="option"][data-variant="1"]').getAttribute('aria-selected'), 'true');
  } finally {
    await ui.close();
  }
});

test('saving locks only its own capability and queued controls recover on success or failure', async () => {
  const ui = await mount();
  try {
    await act(async () => ui.switch('vision').click());
    assert.equal(ui.switch('vision').disabled, true);
    assert.equal(ui.select('vision').disabled, true);
    for (const tool of tools.filter((tool) => tool !== 'vision')) {
      assert.equal(ui.switch(tool).disabled, false);
      assert.equal(ui.select(tool).disabled, false);
    }
    // A pending switch-off cannot open a model dialog that would disappear on completion.
    await act(async () => ui.select('vision').click());
    assert.equal(document.querySelector('[role="option"][data-variant="configure"]'), null);
    await act(async () => ui.switch('audio').click());
    assert.equal(ui.switch('audio').disabled, true);
    assert.equal(ui.select('audio').disabled, true);
    await act(async () => ui.switch('audio').click());
    assert.equal(ui.writes.length, 1);
    await ui.finish(0);
    assert.equal(ui.switch('vision').disabled, false);
    assert.equal(ui.switch('vision').getAttribute('aria-checked'), 'false');
    assert.equal(ui.select('vision'), null);
    assert.equal(ui.switch('audio').disabled, true);
    assert.equal(ui.writes.length, 2);
    await ui.finish(1, new Error('save failed'));
    assert.equal(ui.switch('audio').getAttribute('aria-checked'), 'true');
    assert.equal(ui.select('audio').disabled, false);
    for (const tool of tools) assert.equal(ui.switch(tool).disabled, false);
    await act(async () => ui.switch('vision').click());
    await ui.finish(2);
    assert.equal(ui.select('vision').disabled, false);
    assert.match(ui.select('vision').textContent, /a/);
    assert.deepEqual(ui.writes.map((write) => write.params), [
      { tool_id: 'vision', enabled: false }, { tool_id: 'audio', enabled: false }, { tool_id: 'vision', enabled: true },
    ]);
  } finally {
    await ui.close();
  }
});

test('saving a model selection locks its own switch while other capabilities can queue', async () => {
  const ui = await mount();
  try {
    await act(async () => ui.select('video').click());
    await act(async () => document.querySelector('[role="option"][data-variant="1"]').click());
    assert.equal(ui.select('video').disabled, true);
    assert.equal(ui.switch('video').disabled, true);
    assert.equal(ui.switch('vision').disabled, false);
    await act(async () => ui.switch('video').click());
    await act(async () => ui.switch('vision').click());
    assert.equal(ui.writes.length, 1);
    await ui.finish(0);
    assert.equal(ui.select('video').disabled, false);
    assert.equal(ui.switch('video').disabled, false);
    assert.match(ui.select('video').textContent, /b/);
    assert.equal(ui.switch('vision').disabled, true);
    await ui.finish(1);
    assert.equal(ui.switch('video').getAttribute('aria-checked'), 'true');
    assert.deepEqual(ui.writes.map((write) => write.params), [
      { tool_id: 'video', model_id: 'b' }, { tool_id: 'vision', enabled: false },
    ]);
  } finally {
    await ui.close();
  }
});

test('a queued save cannot erase the previous failure and explicit retry clears the feedback', async () => {
  const ui = await mount();
  const feedback = () => document.querySelector('[data-testid="settings-multimodal-feedback"]');
  try {
    await act(async () => ui.switch('vision').click());
    await act(async () => ui.switch('audio').click());
    await ui.finish(0, new Error('vision save failed'));
    assert.equal(ui.switch('vision').getAttribute('aria-checked'), 'true');
    assert.equal(ui.switch('vision').disabled, false);
    assert.equal(ui.writes.length, 2);
    assert.ok(feedback());
    await ui.finish(1);
    assert.ok(feedback());
    assert.equal(ui.switch('audio').getAttribute('aria-checked'), 'false');
    await act(async () => document.querySelector('[data-testid="settings-multimodal-retry-btn"]').click());
    assert.equal(feedback(), null);
    assert.equal(ui.switch('vision').getAttribute('aria-checked'), 'true');
  } finally {
    await ui.close();
  }
});

test('a successful reconnect clears a previous read error and restores the controls', async () => {
  let readFails = true;
  const ui = await mount({ failRead: () => readFails });
  const feedback = () => document.querySelector('[data-testid="settings-multimodal-feedback"]');
  try {
    assert.ok(feedback());
    assert.equal(ui.switch('vision').disabled, true);
    await ui.render(false);
    readFails = false;
    await ui.render(true);
    assert.equal(Boolean(feedback()), false);
    assert.equal(ui.switch('vision').disabled, false);
    assert.equal(ui.select('vision').disabled, false);
    assert.equal(ui.switch('vision').getAttribute('aria-checked'), 'true');
  } finally {
    await ui.close();
  }
});

test('stored error causes retain localized backend messages and confirmation handling', async () => {
  for (const language of ['zh', 'en']) {
    await i18n.changeLanguage(language);
    const ui = await mount();
    const feedback = () => document.querySelector('[data-testid="settings-multimodal-feedback"]');
    try {
      await act(async () => ui.switch('vision').click());
      await ui.finish(0, Object.assign(new Error('server rejected save'), {
        code: 'REMOTE_ERROR', payload: { code: 'MM_STORAGE_FAILED' },
      }));
      const messageKey = 'settingsPanel.multimodal.errors.MM_STORAGE_FAILED';
      assert.equal(i18n.exists(messageKey), true);
      assert.ok(feedback().textContent.includes(i18n.t(messageKey)));
      await act(async () => ui.switch('vision').click());
      await ui.finish(1, Object.assign(new Error('confirm references'), {
        payload: { code: 'MM_CONFIRM_REQUIRED', affected_tools: ['vision'] },
      }));
      assert.equal(Boolean(feedback()), false);
      await act(async () => ui.switch('vision').click());
      await ui.finish(2, new Error('transport failure'));
      assert.ok(feedback().textContent.includes(i18n.t('settingsPanel.multimodal.errors.unknown')));
    } finally {
      await ui.close();
    }
  }
});
