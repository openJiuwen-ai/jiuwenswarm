import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildDuplexClearUpdates,
  buildDuplexUpdates,
  createDuplexDraft,
  duplexFieldError,
  readDuplexSettings,
} from '../node_modules/.cache/settings-refactor/modules/agent/a2aDuplexConfig.js';
import { normalizeSettingsConfigUpdates } from '../node_modules/.cache/settings-refactor/services/settingsContract.js';

const saved = {
  duplex_router_enabled: 'true',
  duplex_router_mode: 'active',
  duplex_router_format: 'anthropic',
  duplex_router_api_base: 'https://relay.example/v1',
  duplex_router_model: 'claude-opus',
  duplex_router_jev_typesafe_model: 'jev-1.13.0',
  duplex_router_jev_typesafe_key_configured: 'true',
  duplex_router_jev_openrouter_model: 'typesafe/jev-1.13',
  duplex_router_jev_openrouter_key_configured: 'false',
  duplex_router_timeout_seconds: '3.5',
  duplex_router_api_key: '',
  duplex_router_api_key_configured: 'true',
};

test('config.get values round-trip through the draft to config.save_all keys', () => {
  const view = readDuplexSettings(saved);
  assert.equal(view.hasApiKey, true);
  const draft = createDuplexDraft(view);
  assert.equal(draft.api_key, '');
  const updates = buildDuplexUpdates(draft);
  assert.deepEqual(updates, {
    duplex_router_enabled: 'true',
    duplex_router_mode: 'active',
    duplex_router_format: 'anthropic',
    duplex_router_api_base: 'https://relay.example/v1',
    duplex_router_endpoint: '',
    duplex_router_model: 'claude-opus',
    duplex_router_timeout_seconds: '3.5',
  });
  assert.equal(buildDuplexUpdates({ ...draft, api_key: ' k ' }).duplex_router_api_key, 'k');
  // Jev: url, endpoint and model follow the url host; the own key is optional on shared hosts.
  const jev = createDuplexDraft({ ...view, format: 'jev', api_base: '', model: '' });
  assert.deepEqual([jev.api_base, jev.endpoint, jev.model], ['https://api.typesafe.ai/v1', '/systemone', 'jev-1.13.0']);
  assert.equal(buildDuplexUpdates(jev).duplex_router_endpoint, '/systemone');
  const other = createDuplexDraft({ ...view, format: 'jev', api_base: 'https://mcl.example/v1', model: '' });
  assert.deepEqual([other.endpoint, other.model], ['/decisions', '']);
  const noKey = { ...view, hasApiKey: false };
  assert.equal(duplexFieldError('api_key', jev, noKey), undefined);
  assert.equal(duplexFieldError('api_key', { ...jev, api_base: 'https://openrouter.ai/api/alpha' }, noKey), 'jevKey');
  assert.equal(duplexFieldError('api_key', other, noKey), 'required');
  assert.equal(duplexFieldError('model', { ...jev, model: '' }, noKey), undefined);
  assert.equal(duplexFieldError('model', other, noKey), 'required');
  // A saved own key only counts while the format and url host stay the same (the server drops it otherwise).
  const ownOnOpenRouter = {
    ...view,
    format: 'jev',
    api_base: 'https://openrouter.ai/api/alpha',
    apiKeyEnv: 'DUPLEX_ROUTER_API_KEY',
  };
  const openrouter = { ...jev, api_base: 'https://openrouter.ai/api/alpha' };
  assert.equal(duplexFieldError('api_key', openrouter, ownOnOpenRouter), undefined);
  assert.equal(
    duplexFieldError('api_key', openrouter, { ...ownOnOpenRouter, api_base: 'https://api.typesafe.ai/v1' }),
    'jevKey',
  );
  assert.equal(buildDuplexUpdates({ ...other, api_key: 'k' }).duplex_router_api_key, 'k');
  // Every emitted key is part of the settings contract.
  normalizeSettingsConfigUpdates({ ...updates, ...buildDuplexClearUpdates() });
});

test('clear turns the supervisor off and clears the stored key', () => {
  assert.deepEqual(buildDuplexClearUpdates(), {
    duplex_router_enabled: 'false',
    duplex_router_mode: 'off',
    duplex_router_api_base: '',
    duplex_router_endpoint: '',
    duplex_router_model: '',
    duplex_router_api_key: '',
  });
});
