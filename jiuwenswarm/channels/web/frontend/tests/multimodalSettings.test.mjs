import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { MULTIMODAL_CAPABILITIES, multimodalModelOptions, MultimodalSettingsStore } from '../node_modules/.cache/multimodal-settings/features/settings/services/multimodalSettings.js';
import { SettingsSaveQueue } from '../node_modules/.cache/multimodal-settings/features/settings/services/SettingsSaveQueue.js';
import { FormStore } from '../node_modules/.cache/multimodal-settings/components/form/core/FormStore.js';

function snapshot(tools = {}) {
  return { schema_version: 1, models: [], tools };
}

test('tool options filter capability, retain list order and distinguish duplicate names', () => {
  const models = [
    { model_id: 'a', display_name: 'Shared', model_name: 'vision-a', api_base: 'https://a.invalid', capabilities: ['vision'] },
    { model_id: 'b', display_name: 'Shared', model_name: 'vision-b', api_base: 'https://b.invalid', capabilities: ['vision'] },
    { model_id: 'c', display_name: 'Shared', model_name: 'audio', api_base: 'https://c.invalid', capabilities: ['audio'] },
    { model_id: 'd', display_name: 'Shared', model_name: 'vision-a', api_base: 'https://a.invalid', capabilities: ['vision'] },
    { model_id: 'e', display_name: 'Unique', model_name: 'vision-e', api_base: 'https://e.invalid', capabilities: ['vision'] },
  ];
  const options = multimodalModelOptions(models, 'vision');
  assert.deepEqual(options.map((option) => option.value), ['a', 'b', 'd', 'e']);
  assert.equal(new Set(options.map((option) => option.label)).size, 4);
  assert.match(options[0].label, /vision-a.*https:\/\/a\.invalid/);
  assert.equal(options[3].label, 'Unique');
  assert.deepEqual(multimodalModelOptions(models, 'audio'), [{ value: 'c', label: 'Shared' }]);
});

test('all callers share the saved snapshot and queued writes send ordinary save parameters', async () => {
  const seen = [];
  const tools = {};
  let finishFirst;
  const store = new MultimodalSettingsStore(async (method, params) => {
    if (method.endsWith('.list')) return snapshot();
    seen.push(params);
    if (seen.length === 1) await new Promise((resolve) => { finishFirst = resolve; });
    tools[params.tool_id] = { model_id: null, enabled: params.enabled };
    return { saved: true, changed: true, applied_without_restart: true, snapshot: snapshot({ ...tools }) };
  }, new SettingsSaveQueue());
  await store.load();
  const first = store.write('tools.update', { tool_id: 'vision', enabled: false });
  const second = store.write('tools.update', { tool_id: 'audio', enabled: false });
  await new Promise((resolve) => setImmediate(resolve));
  assert.deepEqual(seen, [{ tool_id: 'vision', enabled: false }]);
  finishFirst();
  await Promise.all([first, second]);
  assert.deepEqual(seen, [{ tool_id: 'vision', enabled: false }, { tool_id: 'audio', enabled: false }]);
  assert.deepEqual(store.getSnapshot().snapshot, snapshot({
    vision: { model_id: null, enabled: false }, audio: { model_id: null, enabled: false },
  }));
});

test('saved but unapplied changes keep server state without showing global success', async () => {
  const queue = new SettingsSaveQueue();
  const states = [];
  queue.subscribe((value) => states.push(value.status));
  const saved = snapshot({ vision: { model_id: 'a', enabled: true } });
  const store = new MultimodalSettingsStore(async (method) => method.endsWith('.list') ? snapshot() : {
    saved: true, changed: true, applied_without_restart: false, snapshot: saved,
  }, queue);
  await store.load();
  const result = await store.write('models.upsert', { mode: 'create' });
  assert.equal(result.saved, true);
  assert.deepEqual(store.getSnapshot().snapshot, saved);
  assert.equal(store.getSnapshot().lastChange.applied_without_restart, false);
  assert.ok(!states.includes('saved'));
  assert.equal(queue.getSnapshot().status, 'idle');
});

test('confirmation failure preserves the actual payload and snapshot', async () => {
  const error = Object.assign(new Error('confirm'), { code: 'MM_CONFIRM_REQUIRED', payload: {
    code: 'MM_CONFIRM_REQUIRED', affected_tools: ['vision', 'audio'],
  } });
  const store = new MultimodalSettingsStore(async (method) => {
    if (method.endsWith('.list')) return snapshot();
    throw error;
  }, new SettingsSaveQueue());
  await store.load();
  await assert.rejects(store.write('models.delete', { model_id: 'a' }), (actual) => actual === error);
  assert.deepEqual(store.getSnapshot().snapshot, snapshot());
  assert.equal(store.getSnapshot().saving, false);
});

test('transport failure keeps the saved snapshot and clears the saving state without announcing success', async () => {
  const queue = new SettingsSaveQueue();
  const states = [];
  queue.subscribe((value) => states.push(value.status));
  const failure = new Error('connection closed before response');
  const store = new MultimodalSettingsStore(async (method) => {
    if (method.endsWith('.list')) return snapshot();
    throw failure;
  }, queue);
  await store.load();
  await assert.rejects(store.write('models.upsert', { mode: 'create' }), (error) => error === failure);
  assert.deepEqual(store.getSnapshot().snapshot, snapshot());
  assert.equal(store.getSnapshot().saving, false);
  assert.equal(store.getSnapshot().error, failure);
  assert.equal(store.getSnapshot().lastChange, null);
  assert.ok(!states.includes('saved'));
});

test('a late list cannot overwrite a newer write response', async () => {
  let finishRead;
  const saved = snapshot({ vision: { model_id: 'a', enabled: true } });
  const store = new MultimodalSettingsStore(async (method) => {
    if (method.endsWith('.list')) return new Promise((resolve) => { finishRead = resolve; });
    return { saved: true, changed: true, applied_without_restart: true, snapshot: saved };
  }, new SettingsSaveQueue());
  const read = store.load();
  await store.write('models.upsert', {});
  finishRead(snapshot());
  await read;
  assert.deepEqual(store.getSnapshot().snapshot, saved);
});

test('server field errors remain visible until the user edits and validates the field', async () => {
  const form = new FormStore({ api_base: 'https://original.invalid' });
  form.configure({ api_base: [{ trigger: 'change', validator: (value) => value.startsWith('https://') ? undefined : 'https required' }] }, {});
  form.setErrors({ api_base: 'server rejected this endpoint' });
  assert.equal(form.getFieldState('api_base').error, 'server rejected this endpoint');
  assert.equal(form.hasUnsavedChanges(), false);
  await form.setFieldValue('api_base', 'https://changed.invalid', { trigger: 'change' });
  assert.equal(form.getFieldState('api_base').error, undefined);
  await form.setFieldValue('api_base', 'invalid', { trigger: 'change' });
  assert.equal(form.getFieldState('api_base').error, 'https required');
});

test('each dynamic tool declaration has Chinese and English field titles/descriptions', () => {
  for (const locale of ['zh', 'en']) {
    const resource = JSON.parse(readFileSync(new URL(`../src/i18n/locales/${locale}.json`, import.meta.url)));
    assert.equal(resource.settingsPanel.multimodal.displayName, resource.settingsPanel.models.customName);
    for (const tool of MULTIMODAL_CAPABILITIES) {
      for (const suffix of ['enabled', 'model_id']) {
        for (const key of ['title', 'description']) {
          assert.ok(resource.settingsPanel.fields[`${tool}_${suffix}`]?.[key], `${locale}:${tool}_${suffix}.${key}`);
        }
      }
    }
  }
});
