import test from 'node:test';
import assert from 'node:assert/strict';

import {
  buildTeamRulesUpdates,
  createTeamRulesDraft,
  teamRulesFieldError,
  readTeamRulesSettings,
} from '../node_modules/.cache/settings-refactor/modules/agent/teamRulesConfig.js';
import { normalizeSettingsConfigUpdates } from '../node_modules/.cache/settings-refactor/services/settingsContract.js';

const saved = {
  team_rules_enabled: 'true',
  team_rules_format: 'anthropic',
  team_rules_api_base: 'https://relay.example/v1',
  team_rules_model: 'claude-opus',
  team_rules_api_key: 'sk-test',
  team_rules_jev_api_base: 'https://api.typesafe.ai/v1',
  team_rules_jev_model: 'jev-1.13.0',
  team_rules_jev_key_configured: 'true',
};

test('config.get values round-trip through the draft to config.save_all keys', () => {
  const view = readTeamRulesSettings(saved);
  assert.equal(view.enabled, true);
  assert.deepEqual(view.jev, { apiBase: 'https://api.typesafe.ai/v1', model: 'jev-1.13.0', keyConfigured: true });
  const updates = buildTeamRulesUpdates({ ...createTeamRulesDraft(view), model: ' claude-opus ' });
  assert.deepEqual(updates, {
    team_rules_enabled: 'true',
    team_rules_format: 'anthropic',
    team_rules_api_base: 'https://relay.example/v1',
    team_rules_model: 'claude-opus',
    team_rules_api_key: 'sk-test',
  });
  // Every emitted key is part of the settings contract.
  normalizeSettingsConfigUpdates(updates);
});

test('unknown format falls back to openai and the switch defaults to off', () => {
  const view = readTeamRulesSettings({ team_rules_format: 'grpc' });
  assert.equal(view.format, 'openai');
  assert.equal(view.enabled, false);
});

test('openai / anthropic need url, model and key only while enabled', () => {
  const view = readTeamRulesSettings({});
  const empty = { enabled: true, format: 'openai', api_base: '', model: '', api_key: '' };
  for (const field of ['api_base', 'model', 'api_key']) {
    assert.equal(teamRulesFieldError(field, empty, view), 'required');
    assert.equal(teamRulesFieldError(field, { ...empty, enabled: false }, view), undefined);
  }
  assert.equal(teamRulesFieldError('api_base', { ...empty, api_base: 'relay.example' }, view), 'url');
  assert.equal(teamRulesFieldError('api_base', { ...empty, enabled: false, api_base: 'relay.example' }, view), 'url');
  for (const bad of ['https://user:pw@relay.example/v1', 'https://relay.example/v1#frag', 'ftp://relay.example']) {
    assert.equal(teamRulesFieldError('api_base', { ...empty, api_base: bad }, view), 'url', bad);
  }
  assert.equal(teamRulesFieldError('api_base', { ...empty, api_base: 'https://relay.example/v1' }, view), undefined);
});

test('jev falls back to the browser connection and only needs a key when none is configured', () => {
  const draft = { enabled: true, format: 'jev', api_base: '', model: '', api_key: '' };
  const withKey = readTeamRulesSettings(saved);
  const noKey = readTeamRulesSettings({ ...saved, team_rules_jev_key_configured: 'false' });
  assert.equal(teamRulesFieldError('api_base', draft, noKey), undefined);
  assert.equal(teamRulesFieldError('model', draft, noKey), undefined);
  assert.equal(teamRulesFieldError('api_key', draft, withKey), undefined);
  assert.equal(teamRulesFieldError('api_key', draft, noKey), 'jevKey');
  assert.equal(teamRulesFieldError('api_key', { ...draft, api_key: 'k' }, noKey), undefined);
});
