import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const values = new Map();
globalThis.localStorage = {
  getItem(key) {
    return values.get(key) ?? null;
  },
  removeItem(key) {
    values.delete(key);
  },
  setItem(key, value) {
    values.set(key, String(value));
  },
};
globalThis.window = {
  __JIUWENSWARM_EDITION__: 'personal',
  history: { replaceState() {} },
  location: { pathname: '/', search: '', replace() {} },
};

const {
  EnterpriseEntry,
  buildCustomContext,
  chooseAgentContext,
  isDebugContext,
} = await import('../node_modules/.cache/user-web-entry/EnterpriseEntry.mjs');
const {
  isAuthEntryPath,
  parseLoginAuthSimulate,
} = await import('../node_modules/.cache/user-web-entry/auth/config.js');
const { buildSimulatedEnterpriseContext } = await import('../node_modules/.cache/user-web-entry/auth/simulate/SimulatedAuthProvider.js');

function renderEntry(edition, simulate = false) {
  window.__JIUWENSWARM_EDITION__ = edition;
  window.__JIUWEN_LOGIN_AUTH_SIMULATE__ = simulate;
  return renderToStaticMarkup(React.createElement(EnterpriseEntry, null, React.createElement('div', { id: 'user-web-content' }, 'user web content')));
}

function resetBrowserState() {
  values.clear();
  window.location.pathname = '/';
  window.location.search = '';
}

test('personal edition renders the standalone User Web without enterprise authentication', () => {
  resetBrowserState();
  const html = renderEntry('personal');

  assert.match(html, /user web content/);
  assert.doesNotMatch(html, /ENTERPRISE WORKSPACE/);
});

test('enterprise edition redirects unauthenticated users instead of rendering User Web', () => {
  resetBrowserState();
  const html = renderEntry('enterprise');

  assert.match(html, /ENTERPRISE WORKSPACE/);
  assert.match(html, /正在前往登录页/);
  assert.doesNotMatch(html, /user web content/);
});

test('enterprise edition loads and validates an authorized context before rendering User Web', () => {
  resetBrowserState();
  localStorage.setItem('openjiuwen_access_token', 'manager-token');
  window.location.search = '?user_id=user-1&group_id=group-1&bot_id=bot-1';

  const html = renderEntry('enterprise');

  assert.match(html, /正在加载工作空间/);
  assert.doesNotMatch(html, /user web content/);
});

test('simulated enterprise login uses local defaults without an access token', () => {
  resetBrowserState();
  const context = buildSimulatedEnterpriseContext('');

  assert.equal(context.user.user_id, 'default');
  assert.equal(context.selected.group_id, 'default');
  assert.equal(context.selected.bot_id, 'default');
  assert.doesNotMatch(renderEntry('enterprise', true), /正在前往登录页/);
});

test('simulated enterprise login accepts URL tuple overrides', () => {
  const context = buildSimulatedEnterpriseContext('?user_id=u1&group_id=g1&bot_id=b1');

  assert.equal(context.user.user_id, 'u1');
  assert.equal(context.selected.group_id, 'g1');
  assert.equal(context.selected.bot_id, 'b1');
});

test('LOGIN_AUTH_SIMULATE accepts only booleans and defaults to true', () => {
  assert.equal(parseLoginAuthSimulate(undefined), true);
  assert.equal(parseLoginAuthSimulate('true'), true);
  assert.equal(parseLoginAuthSimulate(' FALSE '), false);
  assert.throws(() => parseLoginAuthSimulate('yes'), /期望 true 或 false/);
});

test('auth entry path guard stops User Web from redirecting /auth to itself', () => {
  assert.equal(isAuthEntryPath('/auth'), true);
  assert.equal(isAuthEntryPath('/auth/'), true);
  assert.equal(isAuthEntryPath('/chat/'), false);
});

test('agent context selection prefers an exact URL tuple and otherwise falls back', () => {
  const contexts = [
    { bot_id: 'agent-1', group_id: 'group-1', user_id: 'user-1', jiuwenclaw_id: 'gw-1', agent_name: 'Agent 1', group_name: 'Group 1' },
    { bot_id: 'agent-2', group_id: 'group-2', user_id: 'user-1', jiuwenclaw_id: 'gw-2', agent_name: 'Agent 2', group_name: 'Group 2' },
  ];

  assert.equal(
    chooseAgentContext(contexts, { botId: 'agent-2', groupId: 'group-2', userId: 'user-1' })?.bot_id,
    'agent-2',
  );
  assert.equal(chooseAgentContext(contexts, { botId: 'agent-2' })?.group_id, 'group-2');
  assert.equal(chooseAgentContext(contexts, { botId: 'removed-agent' })?.bot_id, 'agent-1');
  assert.equal(chooseAgentContext([], { botId: 'agent-2' }), null);
});

test('debug context preserves explicitly entered routing identifiers', () => {
  const debugContext = buildCustomContext(
    { userId: 'debug-user', groupId: 'debug-group', botId: 'debug-bot' },
    'resolved-gateway',
  );

  assert.equal(isDebugContext('?debug_context=1'), true);
  assert.equal(isDebugContext('?debug_context=0'), false);
  assert.equal(debugContext?.group_id, 'debug-group');
  assert.equal(debugContext?.jiuwenclaw_id, 'resolved-gateway');
  assert.equal(debugContext?.bot_id, 'debug-bot');
  assert.equal(debugContext?.user_id, 'debug-user');
});
