import assert from 'node:assert/strict';
import test from 'node:test';

import { MAIN_NAV_STORAGE_KEY, parseStoredMainNav, resolveMainNavAfterRoute } from '../node_modules/.cache/skill-panel-state/features/mainNavigationState.js';
import {
  buildSkillListParams,
  LatestSkillListRequest,
  shouldFetchSkillList,
} from '../node_modules/.cache/skill-panel-state/components/SkillPanel/skillListRequest.js';
import {
  buildSourcePresentationParams,
  sourceSkillIdentity,
  isSourceSkillInstalled,
  skillPresentationName,
} from '../node_modules/.cache/skill-panel-state/features/EnterpriseSkillSourcePanel/installMetadata.js';
import { resolveEnterpriseSourceCount } from '../node_modules/.cache/skill-panel-state/features/EnterpriseSkillSourcePanel/sourceAvailability.js';

test('restores the skills navigation after a same-tab refresh', () => {
  assert.equal(MAIN_NAV_STORAGE_KEY, 'jiuwenswarm.active-main-nav');
  assert.equal(parseStoredMainNav('skills'), 'skills');
});

test('rejects stale or unavailable persisted navigation values', () => {
  assert.equal(parseStoredMainNav('not-a-panel'), 'chat');
  assert.equal(parseStoredMainNav('configpanel', { blocked: ['configpanel'] }), 'chat');
  assert.equal(parseStoredMainNav('updatepanel', { updaterEnabled: false }), 'chat');
});

test('initial chat route preserves restored skills, while real route changes return to work', () => {
  assert.equal(
    resolveMainNavAfterRoute('skills', {
      previousRouteKey: 'chat-session:session-1',
      routeKey: 'chat-session:session-1',
      routeKind: 'chat-session',
    }),
    'skills',
  );
  assert.equal(
    resolveMainNavAfterRoute('skills', {
      previousRouteKey: 'chat-session:session-1',
      routeKey: 'chat-session:session-2',
      routeKind: 'chat-session',
    }),
    'chat',
  );
  assert.equal(
    resolveMainNavAfterRoute('skills', {
      previousRouteKey: 'not-found:/bad',
      routeKey: 'not-found:/bad',
      routeKind: 'not-found',
    }),
    'chat',
  );
});

test('skills.list is always bound to the active session', () => {
  assert.deepEqual(buildSkillListParams('session-42', false), {
    session_id: 'session-42',
    with_installed: true,
  });
  assert.deepEqual(buildSkillListParams('session-42', true), {
    session_id: 'session-42',
    with_installed: true,
    refresh_marketplaces: true,
  });
});

test('an older skills.list response cannot overwrite the newest request', () => {
  const requests = new LatestSkillListRequest();
  const first = requests.begin();
  const second = requests.begin();

  assert.equal(requests.isLatest(first), false);
  assert.equal(requests.isLatest(second), true);
});

test('skill list loading is deduplicated on mount and refreshed on re-entry', () => {
  const base = {
    isActive: true,
    activeTab: 'my',
    currentContext: 'my:user',
    sessionChanged: false,
  };
  assert.equal(shouldFetchSkillList({ ...base, previousContext: null }), true);
  assert.equal(shouldFetchSkillList({ ...base, previousContext: 'my:user' }), false);
  assert.equal(shouldFetchSkillList({ ...base, previousContext: 'inactive' }), true);
  assert.equal(shouldFetchSkillList({ ...base, isActive: false, previousContext: 'my:prebuilt' }), false);
});

test('install and update requests carry marketplace presentation metadata', () => {
  const item = {
    display_name: 'Enterprise Architecture',
    version: '1.0.0',
    owner_display_name: 'Architecture Team',
  };

  assert.deepEqual(buildSourcePresentationParams(item), {
    display_name: 'Enterprise Architecture',
    version: '1.0.0',
    author: 'Architecture Team',
  });
  assert.deepEqual(buildSourcePresentationParams(item, '1.1.0'), {
    display_name: 'Enterprise Architecture',
    version: '1.1.0',
    author: 'Architecture Team',
  });
});

test('a transient source discovery failure does not hide the enterprise marketplace', () => {
  assert.equal(resolveEnterpriseSourceCount(null, null), null);
  assert.equal(resolveEnterpriseSourceCount(2, null), 2);
  assert.equal(resolveEnterpriseSourceCount(null, []), 0);
  assert.equal(resolveEnterpriseSourceCount(null, [{ source_id: 'hub' }]), 1);
});

test('marketplace installation survives different market and package names and failed update checks', () => {
  const market = { source_id: 'customer', skill_id: 'asset-1', name: '中文技能' };
  const installed = { source_id: 'customer', skill_id: 'asset-1', name: 'english-internal-name' };
  assert.equal(sourceSkillIdentity(market), sourceSkillIdentity(installed));
  assert.equal(isSourceSkillInstalled(sourceSkillIdentity(market), new Set(['customer:asset-1']), true, false), true);
  assert.equal(isSourceSkillInstalled(sourceSkillIdentity(market), new Set(), true, true), false);
  assert.equal(isSourceSkillInstalled('other:asset-1', new Set(['customer:asset-1']), true, false), false);
});

test('uninstall presentation uses the Chinese market name while preserving the internal name', () => {
  const skill = { name: 'english-internal-name', market_display_name: '中文技能' };
  assert.equal(skillPresentationName(skill), '中文技能');
  assert.equal(skill.name, 'english-internal-name');
  assert.equal(skillPresentationName({ name: 'legacy-name' }), 'legacy-name');
});
