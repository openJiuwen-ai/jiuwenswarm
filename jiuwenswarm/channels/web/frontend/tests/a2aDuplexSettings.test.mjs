import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildDuplexUpdates,
  createDuplexDraft,
  duplexFieldError,
  readDuplexSettings,
} from '../node_modules/.cache/settings-refactor/modules/agent/a2aDuplexConfig.js';
import { normalizeSettingsConfigUpdates } from '../node_modules/.cache/settings-refactor/services/settingsContract.js';

const saved = {
  duplex_router_enabled: 'true',
  duplex_router_format: 'anthropic',
  duplex_router_api_base: 'https://relay.example/v1',
  duplex_router_model: 'claude-opus',
  duplex_router_api_key: 'sk-test',
  duplex_router_jev_api_base: 'https://api.typesafe.ai/v1',
  duplex_router_jev_model: 'jev-1.13.0',
  duplex_router_jev_key_configured: 'true',
};

test('config.get values round-trip through the draft to config.save_all keys', () => {
  const view = readDuplexSettings(saved);
  assert.equal(view.enabled, true);
  assert.deepEqual(view.jev, { apiBase: 'https://api.typesafe.ai/v1', model: 'jev-1.13.0', keyConfigured: true });
  const updates = buildDuplexUpdates({ ...createDuplexDraft(view), model: ' claude-opus ' });
  assert.deepEqual(updates, {
    duplex_router_enabled: 'true',
    duplex_router_format: 'anthropic',
    duplex_router_api_base: 'https://relay.example/v1',
    duplex_router_model: 'claude-opus',
    duplex_router_api_key: 'sk-test',
  });
  // Every emitted key is part of the settings contract.
  normalizeSettingsConfigUpdates(updates);
});

test('unknown format falls back to openai and the switch defaults to off', () => {
  const view = readDuplexSettings({ duplex_router_format: 'grpc' });
  assert.equal(view.format, 'openai');
  assert.equal(view.enabled, false);
});

test('openai / anthropic need url, model and key only while enabled', () => {
  const view = readDuplexSettings({});
  const empty = { enabled: true, format: 'openai', api_base: '', model: '', api_key: '' };
  for (const field of ['api_base', 'model', 'api_key']) {
    assert.equal(duplexFieldError(field, empty, view), 'required');
    assert.equal(duplexFieldError(field, { ...empty, enabled: false }, view), undefined);
  }
  assert.equal(duplexFieldError('api_base', { ...empty, api_base: 'relay.example' }, view), 'url');
  assert.equal(duplexFieldError('api_base', { ...empty, enabled: false, api_base: 'relay.example' }, view), 'url');
  for (const bad of ['https://user:pw@relay.example/v1', 'https://relay.example/v1#frag', 'ftp://relay.example']) {
    assert.equal(duplexFieldError('api_base', { ...empty, api_base: bad }, view), 'url', bad);
  }
  assert.equal(duplexFieldError('api_base', { ...empty, api_base: 'https://relay.example/v1' }, view), undefined);
});

test('jev falls back to the browser connection and only needs a key when none is configured', () => {
  const draft = { enabled: true, format: 'jev', api_base: '', model: '', api_key: '' };
  const withKey = readDuplexSettings(saved);
  const noKey = readDuplexSettings({ ...saved, duplex_router_jev_key_configured: 'false' });
  assert.equal(duplexFieldError('api_base', draft, noKey), undefined);
  assert.equal(duplexFieldError('model', draft, noKey), undefined);
  assert.equal(duplexFieldError('api_key', draft, withKey), undefined);
  assert.equal(duplexFieldError('api_key', draft, noKey), 'jevKey');
  assert.equal(duplexFieldError('api_key', { ...draft, api_key: 'k' }, noKey), undefined);
});
