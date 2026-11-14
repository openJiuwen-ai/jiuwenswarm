import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import { renderToStaticMarkup } from 'react-dom/server';
import { initReactI18next } from 'react-i18next';

// Initialize React DOM with a browser so textarea input events use the browser event path.
const browser = new JSDOM('<!doctype html>', { url: 'http://localhost' });
globalThis.window = browser.window;
globalThis.document = browser.window.document;
const { createRoot } = await import('react-dom/client');

const { ColleagueProfilePage } = await import('../node_modules/.cache/jiuwenmate-profile/pages/ColleagueProfilePage.js');
const { OfficeDashboardPage } = await import('../node_modules/.cache/jiuwenmate-profile/pages/OfficeDashboardPage.js');
const { ScheduleBoard } = await import('../node_modules/.cache/jiuwenmate-profile/schedules/ScheduleBoard.js');
const { ColleagueDeleteDialog } = await import('../node_modules/.cache/jiuwenmate-profile/components/ColleagueDeleteDialog.js');
const { ImAuthorizeDialog } = await import('../node_modules/.cache/jiuwenmate-profile/components/ImAuthorizeDialog.js');

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'));
const profileStyles = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');
await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh }, en: { translation: en } }, interpolation: { escapeValue: false } });

const colleague = {
  id: 'agent-1',
  name: '小九',
  description: '通用办公助理',
  persona: '角色：群客服智能体\n规则：准确回答问题。',
  tags: ['效率提升'],
  category: 'custom',
  capabilities: {
    model: 'deepseek#0',
    skills: ['old-skill'],
    connectors: [],
    plugins: [],
    knowledge: [],
    starterPrompts: [],
  },
  imConfigured: false,
  configId: 'agent-1',
  connections: [{ channel: 'web', connectionKey: 'web', agentId: 'agent-1', workspaceDir: null }],
  disabled: false,
  joinedAt: '2026-10-14T08:00:00+00:00',
};

const feishuConnection = { channel: 'feishu', connectionKey: 'cli-app', agentId: 'agent-feishu', workspaceDir: null };

const noop = () => undefined;

const catalog = {
  skills: [{ name: 'report-writer', label: '报告', description: '整理周报' }],
  connectors: [{ name: 'feishu', label: '飞书', description: '' }],
  plugins: [],
  knowledge: [{ name: 'topics/description.md', label: 'topics/description.md', description: '' }],
  ready: true,
};

function renderProfile(extra) {
  return renderToStaticMarkup(
    React.createElement(ColleagueProfilePage, {
      colleague,
      resourceCatalog: catalog,
      onSaveSettings: noop,
      onOpenOffice: noop,
      onChat: noop,
      onConfigureIm: noop,
      onConfigureWelink: noop,
      onRemoveConnection: noop,
      onToggleDisabled: noop,
      onDelete: noop,
      ...extra,
    }),
  );
}

async function mountProfile(extra) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  const openedWindows = [];
  dom.window.open = (url) => {
    const popup = {
      closed: false,
      document: dom.window.document.implementation.createHTMLDocument(''),
      location: { href: url },
      close() { this.closed = true; },
    };
    openedWindows.push(popup);
    return popup;
  };
  const timers = new Map();
  let timerId = 0;
  dom.window.setInterval = (callback) => { timers.set(++timerId, callback); return timerId; };
  dom.window.clearInterval = (id) => timers.delete(id);
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  let props = {
    colleague,
    initialSection: 'permissions',
    resourceCatalog: catalog,
    onSaveSettings: noop,
    onOpenOffice: noop,
    onChat: noop,
    onConfigureIm: noop,
    onConfigureWelink: noop,
    onToggleDisabled: noop,
    onDelete: noop,
    ...extra,
  };
  await act(async () => {
    root.render(React.createElement(ColleagueProfilePage, props));
  });
  const find = (id) => container.querySelector(`[data-testid="jiuwenmate-profile-${id}"]`);
  return {
    container,
    openedWindows,
    find,
    async click(id) {
      assert.ok(find(id), `Missing profile ${id}`);
      await act(async () => { find(id).click(); });
    },
    async flush() { await act(async () => {}); },
    async rerender(patch) {
      props = { ...props, ...patch };
      await act(async () => { root.render(React.createElement(ColleagueProfilePage, props)); });
    },
    async tickIntervals() {
      await act(async () => { for (const callback of [...timers.values()]) callback(); });
    },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      if (previous.window === undefined) delete globalThis.window;
      else globalThis.window = previous.window;
      if (previous.document === undefined) delete globalThis.document;
      else globalThis.document = previous.document;
      if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
      else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
    },
  };
}

test('management model save failures stay visible and a successful retry clears the error', async () => {
  let fail = true;
  const view = await mountProfile({
    initialSection: 'model',
    modelCatalog: { models: [{ id: 'deepseek#0', label: 'DeepSeek', provider: 'OpenAI' },
      { id: 'kimi#1', label: 'Kimi', provider: 'OpenAI' }], defaultModelId: '', defaultModelLabel: '' },
    onSaveSettings: async () => { if (fail) throw new Error('Publication refused'); },
  });
  try {
    const style = document.createElement('style');
    style.textContent = profileStyles;
    document.head.appendChild(style);
    await act(async () => view.container.querySelector('input[value="kimi#1"]').click());
    await act(async () => view.container.querySelector('.jiuwenmate-profile__save-button').click());
    const error = view.find('settings-error');
    assert.ok(error, 'A failed management save must show an error');
    assert.equal(error.getAttribute('role'), 'alert');
    assert.match(error.textContent, /Publication refused/);
    assert.equal(window.getComputedStyle(error.parentElement).gridTemplateRows, 'auto minmax(0, 1fr)',
      'The error must take its text height instead of the full content row');
    assert.equal(view.container.querySelector('input[value="kimi#1"]').checked, true);
    fail = false;
    await act(async () => view.container.querySelector('.jiuwenmate-profile__save-button').click());
    assert.equal(view.find('settings-error'), null);
    assert.equal(window.getComputedStyle(view.find('detail').parentElement).gridTemplateRows, 'minmax(0, 1fr)');
  } finally {
    await view.close();
  }
});

test('the persona save button stays mounted after saving and permits retry after failure', async () => {
  let fail = false;
  const saved = [];
  const view = await mountProfile({ initialSection: 'persona', onSaveSettings: async (edit) => {
    if (fail) throw new Error('Persona save failed');
    saved.push(edit);
  } });
  const changePersona = async (value) => {
    const input = view.container.querySelector('.jiuwenmate-profile__persona-editor textarea');
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLTextAreaElement.prototype, 'value').set.call(input, value);
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    });
  };
  try {
    const button = view.container.querySelector('.jiuwenmate-profile__save-button');
    assert.ok(button, 'The persona save action must remain visible even without changes');
    assert.equal(button.disabled, true);
    await changePersona('更新后的人设');
    assert.equal(button.disabled, false);
    await act(async () => button.click());
    assert.deepEqual(saved, [{ persona: '更新后的人设' }]);
    assert.equal(view.container.querySelector('.jiuwenmate-profile__save-button'), button);
    assert.equal(button.disabled, true);
    assert.equal(button.previousElementSibling?.dataset.testid, 'jiuwenmate-profile-saved-notice');
    fail = true;
    await changePersona('再次修改的人设');
    await act(async () => button.click());
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-profile-saved-notice"]'), null);
    assert.equal(button.disabled, false);
    assert.match(view.find('settings-error').textContent, /Persona save failed/);
    assert.equal(view.container.querySelector('.jiuwenmate-profile__persona-editor textarea').value, '再次修改的人设');
  } finally {
    await view.close();
  }
});

test('starter prompts are edited on the persona page with a limit of three and duplicate hints', async () => {
  const saved = [];
  const view = await mountProfile({
    colleague: { ...colleague, capabilities: { ...colleague.capabilities, starterPrompts: ['写周报'] } },
    initialSection: 'persona',
    onSaveSettings: async (edit) => { saved.push(edit); },
  });
  const inputs = () => [...view.container.querySelectorAll('[data-testid="jiuwenmate-starter-prompt-input"]')];
  const type = async (input, value) => {
    await act(async () => {
      Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
      input.dispatchEvent(new window.Event('input', { bubbles: true }));
    });
  };
  const add = () => view.container.querySelector('[data-testid="jiuwenmate-starter-prompts-add"]');
  const save = () => view.container.querySelector('.jiuwenmate-profile__save-button');
  try {
    assert.deepEqual(inputs().map((input) => input.value), ['写周报']);
    await act(async () => add().click());
    await type(inputs()[1], ' 写周报 ');
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-starter-prompt-duplicate"]'));
    assert.equal(save().disabled, true, 'A duplicate that collapses to the saved list is not a change');

    await type(inputs()[1], '写月报');
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-starter-prompt-duplicate"]'), null);
    await act(async () => add().click());
    await type(inputs()[2], '总结会议');
    assert.equal(add().disabled, true);
    assert.equal(save().disabled, false);
    await act(async () => save().click());
    assert.deepEqual(saved.at(-1)?.starterPrompts, ['写周报', '写月报', '总结会议']);
  } finally {
    await view.close();
  }
});

test('the overview keeps DingTalk setup until DingTalk is also configured', () => {
  const authorized = {
    ...colleague,
    imConfigured: true,
    feishuConfigured: true,
    welinkConfigured: true,
  };
  const channels = renderProfile({
    initialSection: 'channels',
    colleague: { ...authorized, connections: [...colleague.connections, feishuConnection] },
  });
  assert.match(channels, /data-variant="channels"/);
  assert.match(channels, /已授权/);
  assert.match(channels, /data-testid="jiuwenmate-profile-im-modify"/);
  assert.match(channels, />修改</);
  assert.match(channels, /data-testid="jiuwenmate-profile-im-remove"/);
  assert.doesNotMatch(channels, /data-testid="jiuwenmate-profile-im-revoke"[^>]*data-variant="feishu"/);
  assert.match(channels, /data-testid="jiuwenmate-profile-im-revoke"[^>]*data-variant="welink"/);
  assert.match(channels, />取消授权</);

  const withNotice = renderProfile({
    initialSection: 'channels',
    colleague: authorized,
    imNotice: { platform: 'feishu', text: '解除连接成功' },
  });
  assert.match(
    withNotice,
    /data-testid="jiuwenmate-profile-im-notice"[^>]*data-variant="feishu"[^>]*>解除连接成功<\/small>\s*<button[^>]*data-testid="jiuwenmate-profile-im-authorize"/,
  );

  const pendingDingTalk = renderProfile({ colleague: authorized });
  assert.match(pendingDingTalk, /data-testid="jiuwenmate-profile-quick-im"/);
  assert.match(pendingDingTalk, /钉钉/);
  const overview = renderProfile({ colleague: { ...authorized, dingtalkConfigured: true } });
  assert.doesNotMatch(overview, /data-testid="jiuwenmate-profile-quick-im"/);
});

test('a permission save without error details shows a fallback, rolls back and clears on section change', async () => {
  const view = await mountProfile({ onSaveSettings: async () => { throw null; } });
  try {
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-profile-permission-option"][data-variant="automatic"]').click());
    assert.match(view.find('settings-error').textContent, /配置保存或发布失败/);
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-profile-permission-option"][data-variant="default"]').getAttribute('aria-pressed'), 'true');
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-profile-section"][data-variant="model"]').click());
    assert.equal(view.find('settings-error'), null);
  } finally {
    await view.close();
  }
});

test('the employee mail page gives a short connection note', () => {
  const markup = renderProfile({ initialSection: 'mail' });
  assert.match(markup, /data-variant="mail"/);
  assert.match(markup, /Agent Mail/);
  assert.match(markup, /为该员工单独连接邮箱，授权后即可在新对话中使用。/);
  assert.doesNotMatch(markup, /OAuth/);
  assert.doesNotMatch(markup, /无需安装 Docker/);
  assert.doesNotMatch(markup, /运行镜像/);
  assert.match(markup, /data-testid="jiuwenmate-profile-mail-setup"/);
  assert.match(markup, /正在检查邮箱连接/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*data-testid="jiuwenmate-profile-mail-start"/);
});

test('DingTalk CLI is automatic and has no per-employee authorization page', () => {
  const markup = renderProfile({ initialSection: 'overview' });
  assert.doesNotMatch(markup, /钉钉工作台 CLI/);
  assert.doesNotMatch(markup, /钉钉设备授权/);
});

function deferred() {
  let resolve;
  const promise = new Promise((done) => { resolve = done; });
  return { promise, resolve };
}

const dingtalkConnection = { channel: 'dingtalk', connectionKey: 'ding-app', agentId: 'agent-ding', workspaceDir: null };
const dingtalkColleague = { ...colleague, connections: [...colleague.connections, dingtalkConnection] };
const dingtalkAuthorizing = { status: 'authorizing', cli_version: '1', operation_id: 'ding-operation' };

test('authorizing a newly bound DingTalk account opens the browser before waiting for the RPC', async () => {
  const pending = deferred();
  const authUrl = 'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=NEW-CODE';
  const view = await mountProfile({
    initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: () => pending.promise,
  });
  try {
    await view.rerender({ colleague: dingtalkColleague });
    const button = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]');
    await act(async () => button.click());
    assert.equal(view.openedWindows.length, 1);
    assert.equal(view.openedWindows[0].location.href, 'about:blank');
    assert.match(view.openedWindows[0].document.body.textContent, /正在发起授权/);
    assert.equal(button.disabled, true);
    await act(async () => pending.resolve({ ...dingtalkAuthorizing, auth_url: authUrl, user_code: 'NEW-CODE' }));
    assert.equal(view.openedWindows[0].location.href, authUrl);
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-cancel"]'));
  } finally { await view.close(); }
});

test('DingTalk account authorization refreshes and separately displays group monitoring', async () => {
  let monitoring = { status: 'discovering', group_count: 0 };
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'connected', cli_version: '1', account: 'Test user', organization: 'Test org', group_monitor: monitoring }),
  });
  try {
    const status = () => view.container.querySelector('[data-testid="jiuwenmate-dingtalk-group-monitor-status"]');
    assert.match(status().textContent, /正在自动发现/);
    assert.match(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-status"]').textContent, /已绑定/);
    monitoring = { status: 'listening', group_count: 2 };
    await view.tickIntervals();
    assert.match(status().textContent, /2 个群/);
    monitoring = { status: 'no_groups', group_count: 0 };
    await view.tickIntervals();
    assert.match(status().textContent, /未发现/);
    monitoring = { status: 'error', group_count: 2 };
    await view.tickIntervals();
    assert.match(status().textContent, /重试/);
    assert.match(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-status"]').textContent, /已绑定/);
  } finally { await view.close(); }
});

test('DingTalk authorization closes its waiting page after an RPC failure and releases the button', async () => {
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: async () => { throw new Error('CLI preparation failed'); },
  });
  try {
    const button = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]');
    await act(async () => button.click());
    assert.equal(view.openedWindows.length, 1);
    assert.equal(view.openedWindows[0].closed, true);
    assert.equal(button.disabled, false);
    assert.match(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-error"]').textContent, /CLI preparation failed/);
  } finally { await view.close(); }
});

for (const popupFailure of ['blocked', 'throws']) test(`DingTalk authorization offers a usable recovery link when the popup ${popupFailure}`, async () => {
  let started = 0;
  const authUrl = 'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=NEW-CODE';
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: async () => { started += 1; return { ...dingtalkAuthorizing, auth_url: authUrl }; },
  });
  try {
    window.open = () => { if (popupFailure === 'throws') throw new Error('Browser bridge unavailable'); return null; };
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]').click());
    assert.equal(started, 1);
    const link = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]');
    assert.equal(link.href, authUrl);
    assert.equal(link.closest('details').open, true);
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-cancel"]'));
  } finally { await view.close(); }
});

for (const terminal of ['cancelled', 'connected', 'error']) test(`DingTalk closes its own authorization window when ${terminal}`, async () => {
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: async () => ({ ...dingtalkAuthorizing, auth_url: 'https://login.dingtalk.com/test' }),
    onCancelDingTalkAuth: async () => ({ status: 'not_connected', cli_version: '1' }),
    onPollDingTalkAuth: async () => ({ status: terminal, cli_version: '1', ...(terminal === 'error' ? { error_code: 'authorization_failed' } : {}) }),
  });
  try {
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]').click());
    if (terminal === 'cancelled') {
      await act(async () => view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-cancel"]').click());
    } else {
      await view.tickIntervals();
    }
    assert.equal(view.openedWindows[0].closed, true);
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]'), null);
  } finally { await view.close(); }
});

for (const transition of ['removed', 'rebound', 'unmounted']) test(`a pending DingTalk begin cannot redirect its window after the connection is ${transition}`, async () => {
  const pending = deferred();
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: () => pending.promise,
  });
  let closed = false;
  try {
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]').click());
    const popup = view.openedWindows[0];
    if (transition === 'unmounted') {
      await view.close(); closed = true;
    } else {
      await view.rerender({ colleague: transition === 'removed' ? colleague : {
        ...dingtalkColleague, connections: [...colleague.connections, { ...dingtalkConnection, agentId: 'replacement-agent' }],
      } });
    }
    await act(async () => pending.resolve({ ...dingtalkAuthorizing, auth_url: 'https://login.dingtalk.com/stale' }));
    assert.equal(popup.closed, true);
    assert.equal(popup.location.href, 'about:blank');
  } finally { if (!closed) await view.close(); }
});

test('closing the waiting DingTalk popup leaves an expanded recovery link and does not start a second login', async () => {
  const pending = deferred();
  let starts = 0;
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: () => { starts += 1; return pending.promise; },
  });
  try {
    const button = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]');
    await act(async () => { button.click(); button.click(); });
    view.openedWindows[0].close();
    await act(async () => pending.resolve({ ...dingtalkAuthorizing, auth_url: 'https://login.dingtalk.com/recover' }));
    assert.equal(starts, 1);
    assert.equal(view.openedWindows.length, 1);
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]').closest('details').open, true);
  } finally { await view.close(); }
});

test('a connected DingTalk account can fill missing display names on focus without authorizing again', async () => {
  let checks = 0;
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ++checks === 1
      ? { status: 'connected', cli_version: '1' }
      : { status: 'connected', cli_version: '1', account: 'Resolved account', organization: 'Resolved organization' },
  });
  try {
    await act(async () => window.dispatchEvent(new window.Event('focus')));
    assert.match(view.container.textContent, /Resolved account/);
    assert.match(view.container.textContent, /Resolved organization/);
    assert.equal(view.openedWindows.length, 0);
  } finally { await view.close(); }
});

test('a failed DingTalk account status check stops showing loading and can recover through authorization', async () => {
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => { throw new Error('Status lookup failed'); },
    onBeginDingTalkAuth: async () => ({ status: 'not_connected', cli_version: '1' }),
  });
  try {
    const card = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth"]');
    assert.match(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-status"]').textContent, /检查钉钉授权失败/);
    assert.doesNotMatch(card.textContent, /正在检查授权状态/);
    assert.match(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-error"]').textContent, /Status lookup failed/);
    const button = card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]');
    assert.equal(button.disabled, false);
    await act(async () => button.click());
    assert.match(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-status"]').textContent, /尚未绑定钉钉用户账号/);
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-error"]'), null);
  } finally { await view.close(); }
});

test('DingTalk account authorization explains its scope and shows loading then the device link', async () => {
  const pending = deferred();
  const authUrl = 'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=TEST-CODE';
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: () => pending.promise,
    onPollDingTalkAuth: async () => dingtalkAuthorizing,
  });
  try {
    const card = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth"]');
    assert.match(card.textContent, /钉钉用户账号授权/);
    assert.match(card.textContent, /群消息.*在线文档.*日历/);
    const button = card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]');
    await act(async () => { button.click(); });
    assert.equal(button.disabled, true);
    assert.match(button.textContent, /正在发起授权/);
    await act(async () => { pending.resolve({ ...dingtalkAuthorizing, auth_url: authUrl, user_code: 'TEST-CODE' }); });
    const link = card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]');
    assert.equal(link.href, authUrl);
    assert.equal(link.target, '_blank');
    assert.match(card.textContent, /TEST-CODE/);
    await view.tickIntervals();
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]').href, authUrl);
  } finally {
    await view.close();
  }
});

test('returning from DingTalk authorization immediately shows the account and organization', async () => {
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ ...dingtalkAuthorizing,
      auth_url: 'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=TEST', user_code: 'TEST' }),
    onPollDingTalkAuth: async () => ({ status: 'connected', cli_version: '1', account: 'Test user', organization: 'Test organization' }),
  });
  try {
    const link = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]');
    assert.equal(link.closest('details')?.open, true, 'A restored authorization offers recovery without starting a new operation');
    await act(async () => { window.dispatchEvent(new window.Event('focus')); });
    const card = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth"]');
    assert.match(card.textContent, /Test user/);
    assert.match(card.textContent, /Test organization/);
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]'), null);
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-code"]'), null);
    assert.ok(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-disconnect"]'));
  } finally { await view.close(); }
});

test('a visible authorization page refreshes immediately before the next timer tick', async () => {
  const response = deferred();
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => dingtalkAuthorizing,
    onPollDingTalkAuth: () => response.promise,
  });
  try {
    await act(async () => {
      document.dispatchEvent(new window.Event('visibilitychange'));
    });
    await act(async () => { response.resolve({ status: 'connected', cli_version: '1', account: 'Visible user', organization: 'Visible organization' }); });
    assert.match(view.container.textContent, /Visible user/);
    assert.match(view.container.textContent, /Visible organization/);
  } finally { await view.close(); }
});

test('returning to a failed authorization rechecks the login without starting another authorization', async () => {
  let completed = false;
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => completed
      ? { status: 'connected', cli_version: '1', account: 'Recovered user', organization: 'Recovered organization' }
      : { status: 'error', cli_version: '1', error_code: 'authorization_failed' },
  });
  try {
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-error"]'));
    completed = true;
    await act(async () => { window.dispatchEvent(new window.Event('focus')); });
    assert.match(view.container.textContent, /Recovered user/);
    assert.match(view.container.textContent, /Recovered organization/);
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-error"]'), null);
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-disconnect"]'));
  } finally { await view.close(); }
});

test('connected DingTalk organization stays visible when its profile omits the account name', async () => {
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'connected', cli_version: '1', organization: 'Actual organization' }),
  });
  try {
    const account = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-account"]');
    assert.ok(account);
    assert.match(account.textContent, /Actual organization/);
    assert.match(account.textContent, /钉钉账号/);
    assert.match(account.textContent, /暂不可用/);
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-disconnect"]'));
  } finally { await view.close(); }
});

test('DingTalk begin remains authorizing when an older initial status completes', async () => {
  const initialStatus = deferred();
  const polls = [];
  const view = await mountProfile({
    colleague: dingtalkColleague,
    initialSection: 'channels',
    onLoadDingTalkAuthStatus: () => initialStatus.promise,
    onBeginDingTalkAuth: async () => dingtalkAuthorizing,
    onPollDingTalkAuth: async (...args) => { polls.push(args); return dingtalkAuthorizing; },
  });
  try {
    await act(async () => {
      view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]').click();
    });
    await act(async () => { initialStatus.resolve({ status: 'not_connected', cli_version: '1' }); });
    const status = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-status"]');
    assert.equal(status.textContent, zh.jiuwenmate.profile.dingtalkAuth.status.authorizing);
    await view.tickIntervals();
    assert.deepEqual(polls, [[colleague.id, 'ding-app', 'ding-operation']]);
  } finally {
    await view.close();
  }
});

test('a transient DingTalk status failure preserves the active authorization and keeps polling', async () => {
  let checks = 0;
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ status: 'not_connected', cli_version: '1' }),
    onBeginDingTalkAuth: async () => ({ ...dingtalkAuthorizing, auth_url: 'https://login.dingtalk.com/pending' }),
    onPollDingTalkAuth: async () => ++checks === 1 ? { status: 'error', cli_version: '1' }
      : { status: 'connected', cli_version: '1', account: 'Verified account', organization: 'Verified organization' },
  });
  try {
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]').click());
    await view.tickIntervals();
    assert.equal(view.openedWindows[0].closed, false);
    assert.ok(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-cancel"]'));
    await view.tickIntervals();
    assert.match(view.container.textContent, /Verified account/);
    assert.equal(view.openedWindows[0].closed, true);
  } finally { await view.close(); }
});

test('DingTalk terminal authorization failure removes the consumed code and explains organization access', async () => {
  const view = await mountProfile({
    colleague: dingtalkColleague, initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => ({ ...dingtalkAuthorizing,
      auth_url: 'https://login.dingtalk.com/oauth2/device/verify.htm?user_code=USED-CODE', user_code: 'USED-CODE' }),
    onPollDingTalkAuth: async () => ({ status: 'error', cli_version: '1', error_code: 'org_access_denied' }),
    onBeginDingTalkAuth: async () => ({ ...dingtalkAuthorizing, operation_id: 'new-operation' }),
  });
  try {
    await view.tickIntervals();
    const card = view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth"]');
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-open"]'), null);
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-code"]'), null);
    assert.match(card.querySelector('[role="alert"]').textContent, /组织.*CLI.*权限/);
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-admin"]').href,
      'https://open-dev.dingtalk.com/fe/old#/developerSettings');
    assert.equal(card.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth-begin"]').disabled, false);
  } finally {
    await view.close();
  }
});

test('removing an authorizing DingTalk connection stops polling and ignores its pending response', async () => {
  const pendingPoll = deferred();
  let polls = 0;
  const view = await mountProfile({
    colleague: dingtalkColleague,
    initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => dingtalkAuthorizing,
    onPollDingTalkAuth: () => { polls += 1; return pendingPoll.promise; },
  });
  try {
    await view.tickIntervals();
    assert.equal(polls, 1);
    await view.rerender({ colleague });
    await act(async () => { pendingPoll.resolve(dingtalkAuthorizing); });
    await view.tickIntervals();
    await view.tickIntervals();
    assert.equal(polls, 1);
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-dingtalk-user-auth"]'), null);
  } finally {
    await view.close();
  }
});

test('DingTalk polling waits for the previous request before sending another', async () => {
  const pendingPoll = deferred();
  let polls = 0;
  const view = await mountProfile({
    colleague: dingtalkColleague,
    initialSection: 'channels',
    onLoadDingTalkAuthStatus: async () => dingtalkAuthorizing,
    onPollDingTalkAuth: () => { polls += 1; return pendingPoll.promise; },
  });
  try {
    await view.tickIntervals();
    await view.tickIntervals();
    await view.tickIntervals();
    assert.equal(polls, 1);
    await act(async () => { pendingPoll.resolve(dingtalkAuthorizing); });
    await view.tickIntervals();
    assert.equal(polls, 2);
  } finally {
    await view.close();
  }
});

test('employee permission settings show the three individual profiles and explain full access', () => {
  const markup = renderProfile({ initialSection: 'permissions' });
  const fullAccess = renderProfile({
    initialSection: 'permissions',
    colleague: { ...colleague, permissionProfile: 'full_access' },
  });

  assert.match(markup, /data-testid="jiuwenmate-profile-permissions"/);
  assert.match(markup, /默认权限/);
  assert.match(markup, /智能审批/);
  assert.match(markup, /完全访问/);
  assert.match(markup, /15 分钟未确认会自动拒绝/);
  assert.match(fullAccess, /宿主 OS 账户可访问的所有文件和联网命令/);
});

test('the employee permissions page lists scoped permanent sharing grants and supports revoking only active grants', async () => {
  const target = { channel: 'feishu', app_id: 'app-a', kind: 'group', id: 'destination', name: '目标群' };
  const active = {
    grant_id: 'grant-active', source_group_id: 'source-a', source_group_name: '来源群', target,
    content_types: ['messages'], source_platform: 'dingtalk',
    created_at: '2026-10-27T01:02:03Z', status: 'active',
  };
  const revoked = { ...active, grant_id: 'grant-revoked', status: 'revoked' };
  let current = { approvals: [], grants: [active, revoked], conversation_id: 'sharing-approvals' };
  const calls = [];
  const profileConfigId = 'sharing-owner-config';
  const sharingApi = {
    async list(configId) { calls.push(['list', configId]); return current; },
    async decide() { throw new Error('not used'); },
    async revoke(configId, grantId) {
      calls.push(['revoke', configId, grantId]);
      current = { ...current, grants: current.grants.map((item) => item.grant_id === grantId ? { ...item, status: 'revoked' } : item) };
      return current;
    },
  };
  const view = await mountProfile({ sharingApi, colleague: { ...colleague, configId: profileConfigId } });
  try {
    assert.ok(view.find('sharing-permissions'));
    assert.equal(calls[0][0], 'list');
    assert.equal(calls[0][1], profileConfigId);
    assert.equal(view.container.querySelectorAll('[data-testid="jiuwenmate-profile-sharing-grant-row"]').length, 2);
    const activeRow = view.container.querySelector('[data-testid="jiuwenmate-profile-sharing-grant-row"][data-variant="grant-active"]');
    const revokedRow = view.container.querySelector('[data-testid="jiuwenmate-profile-sharing-grant-row"][data-variant="grant-revoked"]');
    assert.match(activeRow.textContent, /来源群/);
    assert.match(activeRow.textContent, /目标群/);
    assert.match(activeRow.textContent, /2026/);
    assert.equal(activeRow.querySelector('[data-testid="jiuwenmate-profile-sharing-revoke"]').disabled, false);
    assert.equal(revokedRow.querySelector('[data-testid="jiuwenmate-profile-sharing-revoke"]').disabled, true);
    await view.click('sharing-revoke');
    assert.deepEqual(calls.at(-1), ['revoke', profileConfigId, 'grant-active']);
    const updatedRow = view.container.querySelector('[data-testid="jiuwenmate-profile-sharing-grant-row"][data-variant="grant-active"]');
    assert.match(updatedRow.textContent, /已撤销/);
    assert.equal(updatedRow.querySelector('[data-testid="jiuwenmate-profile-sharing-revoke"]').disabled, true);
  } finally { await view.close(); }
});

test('the employee permissions page fails closed when listing or revoking a sharing grant fails', async () => {
  const grant = {
    grant_id: 'grant-active', source_group_id: 'source-a', source_group_name: '来源群',
    content_types: ['messages'], source_platform: 'feishu',
    target: { channel: 'feishu', app_id: null, kind: 'group', id: 'destination', name: '目标群' },
    created_at: '2026-10-27T01:02:03Z', status: 'active',
  };
  const failedList = await mountProfile({ sharingApi: {
    async list() { throw new Error('offline'); }, async decide() {}, async revoke() {},
  } });
  try {
    assert.ok(failedList.find('sharing-load-error'));
    assert.equal(failedList.find('sharing-grant-row'), null);
  } finally { await failedList.close(); }

  let listCalls = 0;
  const failedRevoke = await mountProfile({ sharingApi: {
    async list() {
      listCalls += 1;
      return { approvals: [], grants: [grant], conversation_id: 'sharing-approvals' };
    },
    async decide() { throw new Error('not used'); },
    async revoke() { throw new Error('offline'); },
  } });
  try {
    await failedRevoke.click('sharing-revoke');
    await failedRevoke.flush();
    assert.ok(failedRevoke.find('sharing-action-error'));
    const row = failedRevoke.container.querySelector('[data-testid="jiuwenmate-profile-sharing-grant-row"]');
    assert.match(row.textContent, /生效中/);
    assert.ok(listCalls >= 2, 'A failed revoke is reconciled by refreshing the persisted state');
  } finally { await failedRevoke.close(); }
});

test('the employee mail page shows the connected mailbox address', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(React.createElement(ColleagueProfilePage, {
        colleague,
        initialSection: 'mail',
        resourceCatalog: catalog,
        onSaveSettings: noop,
        onOpenOffice: noop,
        onChat: noop,
        onConfigureIm: noop,
        onToggleDisabled: noop,
        onDelete: noop,
        onLoadAgentMailStatus: async () => ({ status: 'connected', cli_version: '1.0.18', email: 'employee@example.com' }),
      }));
      await Promise.resolve();
    });

    assert.match(container.innerHTML, /data-testid="jiuwenmate-profile-mail-address"/);
    assert.match(container.innerHTML, /绑定邮箱：/);
    assert.match(container.innerHTML, /employee@example\.com/);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
    if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});

test('reloading an expired Agent Mail authorization clears the cancel state', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  let statusCall = 0;
  const props = {
    colleague,
    initialSection: 'mail',
    resourceCatalog: catalog,
    onSaveSettings: noop,
    onOpenOffice: noop,
    onChat: noop,
    onConfigureIm: noop,
    onToggleDisabled: noop,
    onDelete: noop,
    onLoadAgentMailStatus: async () => {
      statusCall += 1;
      return statusCall === 1
        ? { status: 'authorizing', cli_version: '1.0.18', operation_id: 'old-operation' }
        : { status: 'not_connected', cli_version: '1.0.18' };
    },
  };
  try {
    await act(async () => {
      root.render(React.createElement(ColleagueProfilePage, props));
      await Promise.resolve();
    });
    assert.ok(container.querySelector('[data-testid="jiuwenmate-profile-mail-cancel"]'));

    await act(async () => {
      root.render(React.createElement(ColleagueProfilePage, { ...props, connectionEpoch: 1 }));
      await new Promise((resolve) => setImmediate(resolve));
    });

    assert.equal(container.querySelector('[data-testid="jiuwenmate-profile-mail-cancel"]'), null);
    assert.ok(container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]'));
    assert.match(container.querySelector('[data-testid="jiuwenmate-profile-mail-status"]').textContent, /未连接/);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
    if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});

test('a stale Agent Mail poll refreshes status instead of staying in cancel state', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  dom.window.open = () => null;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(React.createElement(ColleagueProfilePage, {
        colleague,
        initialSection: 'mail',
        resourceCatalog: catalog,
        onSaveSettings: noop,
        onOpenOffice: noop,
        onChat: noop,
        onConfigureIm: noop,
        onToggleDisabled: noop,
        onDelete: noop,
        onLoadAgentMailStatus: async () => ({ status: 'not_connected', cli_version: '1.0.18' }),
        onBeginAgentMailAuth: async () => ({ status: 'authorizing', operation_id: 'stale-operation' }),
        onPollAgentMailAuth: async () => { throw new Error('Agent Mail runtime operation failed'); },
      }));
      await Promise.resolve();
    });
    await act(async () => {
      container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]').click();
      await new Promise((resolve) => setImmediate(resolve));
    });
    assert.ok(container.querySelector('[data-testid="jiuwenmate-profile-mail-cancel"]'));

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2600));
    });

    assert.equal(container.querySelector('[data-testid="jiuwenmate-profile-mail-cancel"]'), null);
    assert.ok(container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]'));
    assert.equal(container.querySelector('[role="alert"]'), null);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
    if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});

test('closing the tracked Agent Mail authorization tab cancels the pending operation', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  const openedUrls = [];
  let popup;
  let cancelCalls = 0;
  dom.window.open = (url) => {
    openedUrls.push(url);
    popup = {
      closed: false,
      close() {},
      document: { title: '', body: { textContent: '' } },
      location: { set href(value) { openedUrls.push(value); } },
    };
    return popup;
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(React.createElement(ColleagueProfilePage, {
        colleague,
        initialSection: 'mail',
        resourceCatalog: catalog,
        onSaveSettings: noop,
        onOpenOffice: noop,
        onChat: noop,
        onConfigureIm: noop,
        onToggleDisabled: noop,
        onDelete: noop,
        onLoadAgentMailStatus: async () => ({ status: 'not_connected', cli_version: '1.0.18' }),
        onBeginAgentMailAuth: async () => ({
          status: 'authorizing',
          operation_id: 'operation-1',
          auth_url: 'https://agent.qq.com/oauth',
          frontend_opens_auth: true,
        }),
        onCancelAgentMailAuth: async () => {
          cancelCalls += 1;
          return { status: 'not_connected', cli_version: '1.0.18' };
        },
        onPollAgentMailAuth: async () => ({ status: 'authorizing', cli_version: '1.0.18' }),
      }));
      await Promise.resolve();
    });

    await act(async () => {
      container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]').click();
      await new Promise((resolve) => setImmediate(resolve));
    });

    assert.deepEqual(openedUrls, ['about:blank', 'https://agent.qq.com/oauth']);
    popup.closed = true;

    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 2600));
    });

    assert.ok(container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]'));
    assert.equal(container.querySelector('[data-testid="jiuwenmate-profile-mail-cancel"]'), null);
    assert.equal(cancelCalls, 1);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
    if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});

test('closing the pending Agent Mail tab before the URL arrives never opens the OAuth page', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  const openedUrls = [];
  let popup;
  let resolveAuthorization;
  let cancelCalls = 0;
  dom.window.open = (url) => {
    openedUrls.push(url);
    popup = {
      closed: false,
      close() {},
      document: { title: '', body: { textContent: '' } },
      location: { set href(value) { openedUrls.push(value); } },
    };
    return popup;
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  try {
    await act(async () => {
      root.render(React.createElement(ColleagueProfilePage, {
        colleague,
        initialSection: 'mail',
        resourceCatalog: catalog,
        onSaveSettings: noop,
        onOpenOffice: noop,
        onChat: noop,
        onConfigureIm: noop,
        onToggleDisabled: noop,
        onDelete: noop,
        onLoadAgentMailStatus: async () => ({ status: 'not_connected', cli_version: '1.0.18' }),
        onBeginAgentMailAuth: () => new Promise((resolve) => { resolveAuthorization = resolve; }),
        onCancelAgentMailAuth: async () => {
          cancelCalls += 1;
          return { status: 'not_connected', cli_version: '1.0.18' };
        },
      }));
      await Promise.resolve();
    });

    await act(async () => {
      container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]').click();
      await Promise.resolve();
    });
    assert.match(popup.document.body.textContent, /正在准备邮箱授权/);
    popup.closed = true;

    await act(async () => {
      resolveAuthorization({
        status: 'authorizing',
        operation_id: 'operation-2',
        auth_url: 'https://agent.qq.com/oauth',
        frontend_opens_auth: true,
      });
      await new Promise((resolve) => setImmediate(resolve));
    });

    assert.deepEqual(openedUrls, ['about:blank']);
    assert.equal(cancelCalls, 1);
    assert.equal(container.querySelector('[data-testid="jiuwenmate-profile-mail-cancel"]'), null);
    assert.ok(container.querySelector('[data-testid="jiuwenmate-profile-mail-start"]'));
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
    if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});

test('the employee mail page does not explain a duplicate mailbox as a blocking dialog', () => {
  const markup = renderProfile({ initialSection: 'mail' });
  assert.doesNotMatch(markup, /role="alertdialog"/);
  assert.doesNotMatch(markup, /一个邮箱只能绑定给一位员工/);
});

test('the overview shows quick IM setup when no channel is configured', () => {
  const overview = renderProfile({ colleague: { ...colleague, imConfigured: false } });
  assert.match(overview, /data-testid="jiuwenmate-profile-quick-im"/);
  assert.match(overview, />授权</);
  assert.doesNotMatch(overview, /去配置/);
});

test('WeLink keeps its own setup card with a hint and an authorize action in the quick IM block', () => {
  const overview = renderProfile({ colleague: { ...colleague, imConfigured: false } });
  const rows = overview.match(/<div class="jiuwenmate-profile__im-row">[\s\S]*?<\/div>/g) ?? [];
  const welinkRow = rows.find((row) => row.includes('<strong>WeLink</strong>'));
  assert.ok(welinkRow, 'the quick IM block should show a WeLink card');
  assert.match(welinkRow, /连接 WeLink 后，可在群里 @ 这位员工分派任务/);
  assert.match(welinkRow, /jiuwenmate-link-button--blue/);
  assert.match(welinkRow, />授权</);
});

test('skills can be changed on the skills page without the pencil', () => {
  const markup = renderProfile({ initialSection: 'skills' });
  assert.match(markup, /data-testid="jiuwenmate-profile-detail"[^>]*data-variant="skills"/);
  assert.match(markup, /添加技能/);
  assert.match(markup, /old-skill/);
  assert.match(markup, /class="jiuwenmate-profile__resource-grid"/);
  assert.match(markup, /data-testid="jiuwenmate-profile-resource-card"/);
  assert.match(markup, /暂无描述/);
  assert.doesNotMatch(markup, /jiuwenmate-profile__resource-picker/);
  assert.doesNotMatch(markup, /data-testid="jiuwenmate-profile-edit"/);
  assert.doesNotMatch(markup, /AI 员工名称/);
});

test('unconfigured and configured employees show the same save controls', () => {
  const unconfigured = { ...colleague, currentVersion: null,
    capabilities: { ...colleague.capabilities, model: '' } };
  const resources = renderProfile({ colleague: unconfigured, initialSection: 'skills' });
  assert.match(resources, /data-testid="jiuwenmate-profile-resource-save"[^>]*>保存并发布</);
  const overview = renderProfile({ colleague: unconfigured, initialSection: 'overview', initialEditing: true });
  assert.match(overview, />保存并发布</);
  assert.doesNotMatch(resources + overview, /保存草稿/);
});

test('configuration pages use the design-specific persona, model, and resource layouts', () => {
  const persona = renderProfile({ initialSection: 'persona' });
  assert.match(persona, /class="jiuwenmate-profile__persona-editor"/);
  assert.match(persona, /data-section="persona"/);
  assert.match(persona, /角色：群客服智能体/);
  assert.doesNotMatch(persona, /value="通用办公助理"/);
  assert.match(profileStyles, /data-section\]:not\(\[data-section='overview'\]\) > \.jiuwenmate-profile__header/);
  assert.match(profileStyles, /\.jiuwenmate-profile__persona-editor textarea[\s\S]*?min-height: 500px/);

  const model = renderProfile({
    initialSection: 'model',
    modelCatalog: { models: [{ id: 'deepseek#0', label: 'DeepSeek V3', provider: 'DeepSeek' }], defaultModelId: '', defaultModelLabel: '' },
  });
  assert.match(model, /class="jiuwenmate-profile__model-list"/);
  assert.match(model, /class="jiuwenmate-profile__model-icon"/);
  assert.match(model, /选择模型/);
  assert.doesNotMatch(model, /name="jiuwenmate-profile-model"[^>]*disabled/);
  assert.match(model, /jiuwenmate-profile__save-button/);
  assert.doesNotMatch(model, /data-testid="jiuwenmate-profile-edit"/);
  assert.match(model, /DeepSeek V3/);

  for (const section of ['skills', 'connectors', 'plugins']) {
    const markup = renderProfile({
      initialSection: section,
      colleague: { ...colleague, capabilities: { ...colleague.capabilities, [section]: ['configured-item'] } },
    });
    assert.match(markup, /class="jiuwenmate-profile__resource-grid"/);
  }

  const knowledgeEmpty = renderProfile({ initialSection: 'knowledge' });
  assert.match(knowledgeEmpty, /class="jiuwenmate-profile__knowledge-empty"/);
  const knowledgePopulated = renderProfile({
    initialSection: 'knowledge',
    colleague: { ...colleague, capabilities: { ...colleague.capabilities, knowledge: ['topics/description.md'] } },
  });
  assert.match(knowledgePopulated, /class="jiuwenmate-profile__knowledge-list"/);
});

test('an empty employee model page explains setup and opens model settings', async () => {
  let opens = 0;
  const view = await mountProfile({
    colleague: { ...colleague, capabilities: { ...colleague.capabilities, model: '' } },
    initialSection: 'model',
    modelCatalog: { models: [], defaultModelId: '', defaultModelLabel: '' },
    onOpenModelSettings: () => { opens += 1; },
  });
  try {
    assert.ok(view.find('model-empty'));
    assert.match(view.find('model-empty').textContent, /尚未配置模型/);
    assert.match(view.find('model-empty').textContent, /设置.*模型/);
    assert.equal(view.container.querySelector('.jiuwenmate-profile__save-button'), null);
    await view.click('model-add');
    assert.equal(opens, 1);
    await view.rerender({ modelCatalog: {
      models: [{ id: 'deepseek#0', label: 'DeepSeek', provider: 'DeepSeek' }],
      defaultModelId: 'deepseek#0', defaultModelLabel: 'DeepSeek',
    } });
    assert.equal(view.find('model-empty'), null);
    assert.ok(view.container.querySelector('input[value="deepseek#0"]'));
    assert.ok(view.container.querySelector('.jiuwenmate-profile__save-button'));
  } finally {
    await view.close();
  }
});

test('schedules can be created on the schedules page', () => {
  const markup = renderProfile({
    initialSection: 'schedules',
    schedules: [
      {
        id: 'plan-1',
        agentId: 'agent-1',
        name: '晨报',
        prompt: '汇总昨日事项',
        cronExpr: '0 9 * * *',
        timezone: 'Asia/Shanghai',
        enabled: true,
        nextFireAt: '2026-10-18T01:00:00+00:00',
        scheduleKind: 'cron',
        intervalSeconds: null,
        runAt: null,
        startsAt: null,
        endsAt: null,
        conversationId: 'schedule:plan-1',
        runCount: 0,
        lastStatus: 'running',
      },
    ],
    onCreateSchedule: noop,
    onSetScheduleEnabled: noop,
    onDeleteSchedule: noop,
  });
  assert.match(markup, /data-variant="schedules"/);
  assert.match(markup, /晨报/);
  assert.match(markup, /每天 9:00/);
  assert.match(markup, /添加/);
  assert.match(markup, /jiuwenmate-schedule-section__bar[\s\S]*?<h2>定时任务<\/h2>[\s\S]*?data-testid="jiuwenmate-schedule-add"/);
  assert.match(markup, /data-testid="jiuwenmate-schedule-add"/);
  assert.match(markup, /class="jiuwenmate-outline-button jiuwenmate-schedule-add"[^>]*data-testid="jiuwenmate-schedule-add"/);
  assert.match(markup, /lucide-plus/);
  assert.match(markup, /class="jiuwenmate-schedule-card"/);
  assert.match(markup, /jiuwenmate-schedule-card__actions/);
  assert.match(markup, /role="switch"/);
  assert.match(markup, /class="jiuwenmate-switch__thumb"/);
});

test('schedule board keeps the executor avatar color selected for the colleague', () => {
  const markup = renderToStaticMarkup(
    React.createElement(ScheduleBoard, {
      schedules: [
        {
          id: 'plan-1', agentId: 'agent-1', name: '晨报', prompt: '汇总昨日事项', cronExpr: '0 9 * * *', timezone: 'Asia/Shanghai',
          enabled: true, nextFireAt: '2026-10-18T01:00:00+00:00', scheduleKind: 'cron', intervalSeconds: null, runAt: null,
          startsAt: null, endsAt: null, conversationId: 'schedule:plan-1', runCount: 0, lastStatus: 'running',
        },
      ],
      colleagues: [{ id: 'agent-1', name: '小九', category: 'custom', avatarHue: 230 }],
      onCreate: noop,
      onUpdate: noop,
      onSetEnabled: noop,
      onDelete: noop,
      onRun: noop,
      onLoadHistory: async () => ({ plan: null, runs: [] }),
    }),
  );
  assert.match(markup, /data-hue="230"[^>]*data-testid="jiuwenmate-colleague-avatar"/);
  assert.match(markup, /--jm-avatar-hue:230deg/);
});

test('schedule board keeps the default executor portrait unfiltered', () => {
  const markup = renderToStaticMarkup(
    React.createElement(ScheduleBoard, {
      schedules: [
        {
          id: 'plan-default', agentId: 'default-agent', name: '晨报', prompt: '汇总昨日事项', cronExpr: '0 9 * * *', timezone: 'Asia/Shanghai',
          enabled: true, nextFireAt: '2026-10-18T01:00:00+00:00', scheduleKind: 'cron', intervalSeconds: null, runAt: null,
          startsAt: null, endsAt: null, conversationId: 'schedule:plan-default', runCount: 0, lastStatus: 'running',
        },
      ],
      colleagues: [{ id: 'default-agent', name: '小九', category: 'custom', isDefault: true }],
      onCreate: noop,
      onUpdate: noop,
      onSetEnabled: noop,
      onDelete: noop,
      onRun: noop,
      onLoadHistory: async () => ({ plan: null, runs: [] }),
    }),
  );
  assert.doesNotMatch(markup, /data-hue=/);
});

test('the overview shows the employee activity instead of an empty log', () => {
  const markup = renderProfile({
    today: new Date('2026-10-19T12:00:00+08:00'),
    activity: {
      agentId: 'agent-1',
      calls: 1,
      tokens: 12,
      tasks: 2,
      replies: 1,
      failures: 1,
      companionDays: 6,
      lastResponseAt: '2026-10-16T10:01:00+00:00',
      days: [{ date: '2026-10-16', tasks: 2, minutes: 1, calls: 1, tokens: 12 }],
      kinds: [{ date: '2026-10-16', conversation: 1, channel: 0, schedule: 1 }],
      history: [
        {
          requestId: 'req-plan',
          title: '喝水提醒',
          kind: 'schedule',
          channel: 'web',
          status: 'completed',
          tokens: 12,
          updatedAt: '2026-10-16T03:01:00+00:00',
          startedAt: '2026-10-16T03:00:00+00:00',
        },
      ],
      hours: [],
      channels: [{ channel: 'web', calls: 1, tokens: 12, tasks: 2, replies: 1, failures: 1 }],
      groups: [],
    },
  });
  assert.match(markup, /data-testid="jiuwenmate-profile-metrics"/);
  assert.match(markup, /data-level="1"[^>]*data-variant="2026-10-16"/);
  assert.match(markup, /data-testid="jiuwenmate-profile-task-count"[\s\S]*?<strong>2<\/strong>/);
  assert.match(markup, /data-kind="schedule"[\s\S]*?<strong>1<\/strong>/);
  assert.match(markup, /data-testid="jiuwenmate-profile-history-row"[\s\S]*?喝水提醒/);
  assert.match(markup, /已完成/);
  assert.doesNotMatch(markup, /暂无执行记录/);
});

test('the pencil edits the current page and stays there', () => {
  const overview = renderProfile({ initialSection: 'overview', initialEditing: true });
  assert.match(overview, /aria-current="true"[^>]*data-variant="overview"/);
  assert.match(overview, /aria-label="编辑人设"/);
  assert.match(overview, /data-testid="jiuwenmate-profile-name-input"/);
  assert.doesNotMatch(overview, /data-testid="jiuwenmate-profile-detail"/);
  assert.doesNotMatch(overview, /class="jiuwenmate-profile__description"/);
  assert.match(renderProfile({ initialSection: 'overview' }), /class="jiuwenmate-profile__description"/);

  const model = renderProfile({
    initialSection: 'model',
    initialEditing: true,
    modelCatalog: { models: [{ id: 'deepseek#0', label: 'DeepSeek', provider: 'DeepSeek' }], defaultModelId: 'deepseek#0', defaultModelLabel: 'DeepSeek' },
  });
  assert.match(model, /aria-current="true"[^>]*data-variant="model"/);
  assert.doesNotMatch(model, /aria-label="编辑模型"/);
  assert.match(model, /data-testid="jiuwenmate-profile-detail"[^>]*data-variant="model"/);
  assert.match(model, /type="radio"/);
});

test('knowledge uses the compact page list without exposing the office directory', () => {
  const markup = renderProfile({
    initialSection: 'knowledge',
    colleague: {
      ...colleague,
      workspaceDir: '/tmp/office',
      capabilities: { ...colleague.capabilities, knowledge: ['topics/description.md'] },
    },
  });
  assert.match(markup, /data-variant="knowledge"/);
  assert.match(markup, /添加知识/);
  assert.match(markup, /topics\/description\.md/);
  assert.match(markup, /class="jiuwenmate-profile__knowledge-list"/);
  assert.doesNotMatch(markup, /\/tmp\/office/);
});

test('an empty knowledge catalog tells the user to publish a page first', () => {
  const markup = renderProfile({
    initialSection: 'knowledge',
    resourceCatalog: { ...catalog, knowledge: [] },
  });
  assert.match(markup, /添加知识/);
  assert.match(markup, /没有可装载的已发布知识页面/);
  assert.match(markup, /左侧「资产」/);
});

test('channel reply mode renders a compact selected state and disables changes while saving', () => {
  const html = renderProfile({ initialSection: 'channels', colleague: { ...colleague, groupReplyMode: 'passive' } });
  assert.match(html, /aria-pressed="true"[^>]*data-variant="passive"/);
  assert.match(html, /aria-pressed="false"[^>]*data-variant="adaptive"/);
  assert.match(html, /钉钉绑定用户账号后，会定期检查已接入群内正文不含 @ 的新增文字/);
  const saving = renderProfile({ initialSection: 'channels', savingResources: true });
  assert.match(saving, /aria-pressed="true"[^>]*disabled=""/);
});


test('each bound app is listed with its own platform, and another app can be added', () => {
  const dingtalk = { channel: 'dingtalk', connectionKey: 'ding-app', agentId: 'agent-ding', workspaceDir: null };
  const markup = renderProfile({
    initialSection: 'channels',
    onRemoveConnection: noop,
    colleague: { ...colleague, imConfigured: true, connections: [...colleague.connections, feishuConnection, dingtalk] },
  });
  assert.match(markup, /钉钉/);
  assert.match(markup, /飞书/);
  assert.match(markup, /data-testid="jiuwenmate-profile-connection" data-variant="cli-app"/);
  assert.match(markup, /data-testid="jiuwenmate-profile-connection" data-variant="ding-app"/);
  assert.equal(markup.match(/data-testid="jiuwenmate-profile-im-remove"/g).length, 2);
  assert.match(markup, /data-testid="jiuwenmate-profile-im-add"/);
  assert.doesNotMatch(markup, /data-testid="jiuwenmate-profile-quick-im"/);

  const unbound = renderProfile({ initialSection: 'channels' });
  assert.doesNotMatch(unbound, /data-testid="jiuwenmate-profile-connection"/);
  assert.match(unbound, />授权</);
});


test('deleting a colleague offers to keep its data or delete everything, keeping data by default', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  const chosen = [];
  try {
    await act(async () => {
      root.render(React.createElement(ColleagueDeleteDialog, {
        name: '会计助手', onCancel: noop, onConfirm: (mode) => chosen.push(mode),
      }));
    });
    const modes = [...container.querySelectorAll('[data-testid="jiuwenmate-delete-mode-input"]')];
    assert.deepEqual(modes.map((input) => [input.value, input.checked]), [['unbind', true], ['purge', false]]);
    assert.equal(container.querySelector('[data-testid="jiuwenmate-delete-purge-warning"]'), null);
    await act(async () => container.querySelector('[data-testid="jiuwenmate-delete-confirm"]').click());
    await act(async () => modes[1].click());
    assert.ok(container.querySelector('[data-testid="jiuwenmate-delete-purge-warning"]'));
    await act(async () => container.querySelector('[data-testid="jiuwenmate-delete-confirm"]').click());
    assert.deepEqual(chosen, ['unbind', 'purge']);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    if (previous.window === undefined) delete globalThis.window;
    else globalThis.window = previous.window;
    if (previous.document === undefined) delete globalThis.document;
    else globalThis.document = previous.document;
    if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
    else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});

async function mountImAuthorize(extra) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  await act(async () => {
    root.render(React.createElement(ImAuthorizeDialog, { onSkip: noop, created: false, ...extra }));
  });
  const find = (id) => container.querySelector(`[data-testid="jiuwenmate-im-${id}"]`);
  return {
    find,
    async fill(id, value) {
      await act(async () => {
        const input = find(id);
        Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set.call(input, value);
        input.dispatchEvent(new window.Event('input', { bubbles: true }));
      });
    },
    async confirm() { await act(async () => find('confirm').click()); },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      globalThis.window = previous.window;
      globalThis.document = previous.document;
      if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
      else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
    },
  };
}

for (const [language, platform, message] of [
  ['zh', 'feishu', '该渠道已绑定其他员工，请先解除原绑定。'],
  ['en', 'dingtalk', 'This channel is already bound to another employee. Remove the existing binding first.'],
]) {
  test(`IM binding conflicts show a ${language} message and preserve ${platform} credentials for retry`, async () => {
    await i18n.changeLanguage(language);
    const submissions = [];
    const view = await mountImAuthorize({
      initialPlatform: platform,
      onConfirm: async (credentials) => {
        submissions.push(credentials);
        if (submissions.length === 1) {
          throw Object.assign(new Error('Internal binding conflict'), { code: 'CHANNEL_ALREADY_BOUND' });
        }
      },
    });
    try {
      await view.fill('app-id', ' shared-id ');
      await view.fill('secret', 'retry-secret');
      if (platform === 'dingtalk') await view.fill('robot-code', ' robot-code ');
      await view.confirm();
      assert.equal(view.find('error').getAttribute('role'), 'alert');
      assert.equal(view.find('error').textContent, message);
      assert.equal(view.find('app-id').value, ' shared-id ');
      assert.equal(view.find('secret').value, 'retry-secret');
      assert.equal(view.find('platform-select').value, platform);
      if (platform === 'dingtalk') assert.equal(view.find('robot-code').value, ' robot-code ');
      assert.equal(view.find('confirm').disabled, false);
      await view.confirm();
      assert.equal(view.find('error'), null);
      const credentials = { platform, appId: 'shared-id', appSecret: 'retry-secret', robotCode: platform === 'dingtalk' ? 'robot-code' : '' };
      assert.deepEqual(submissions, [credentials, credentials]);
    } finally {
      await view.close();
      await i18n.changeLanguage('zh');
    }
  });
}

test('IM authorization keeps the original handling for unrelated errors', async () => {
  for (const [cause, message] of [
    [Object.assign(new Error('Credentials rejected'), { code: 'INVALID_CREDENTIALS' }), 'Credentials rejected'],
    ['Service unavailable', 'Service unavailable'],
    [null, 'null'],
  ]) {
    const view = await mountImAuthorize({ onConfirm: async () => { throw cause; } });
    try {
      await view.fill('app-id', 'cli-app');
      await view.fill('secret', 'secret');
      await view.confirm();
      assert.equal(view.find('error').textContent, message);
      assert.equal(view.find('confirm').disabled, false);
    } finally { await view.close(); }
  }
});

test('WeLink mail card explains missing binding and opens existing WeLink login', async () => {
  let logins = 0;
  const view = await mountProfile({
    initialSection: 'mail',
    onLoadWelinkOfficeStatus: async () => ({ status: 'unavailable', reason: 'WELINK_NOT_BOUND' }),
    onConfigureWelink: () => { logins += 1; },
  });
  try {
    assert.ok(view.find('mail-setup'));
    assert.match(view.find('welink-mail-hint').textContent, /WeLink/);
    assert.equal(view.find('welink-mail-connect').disabled, true);
    await view.click('welink-mail-login');
    assert.equal(logins, 1);
  } finally { await view.close(); }
});

test('WeLink mail connection verifies email, shows account and disconnects', async () => {
  const calls = [];
  const view = await mountProfile({
    initialSection: 'mail',
    onLoadWelinkOfficeStatus: async () => ({ status: 'not_connected', email: 'employee@example.com' }),
    onConnectWelinkOffice: async (id, email) => {
      calls.push([id, email]);
      return { status: 'connected', email, account: 'welink-employee' };
    },
    onDisconnectWelinkOffice: async (id) => { calls.push([id]); return { status: 'not_connected' }; },
  });
  try {
    await view.click('welink-mail-connect');
    assert.deepEqual(calls[0], [colleague.id, 'employee@example.com']);
    assert.match(view.find('welink-mail-account').textContent, /welink-employee/);
    await view.click('welink-mail-disconnect');
    assert.deepEqual(calls[1], [colleague.id]);
    assert.equal(view.find('welink-mail-disconnect'), null);
  } finally { await view.close(); }
});

test('WeLink mail keeps the form retryable after a failed verification', async () => {
  const view = await mountProfile({
    initialSection: 'mail',
    onLoadWelinkOfficeStatus: async () => ({ status: 'not_connected', email: 'employee@example.com' }),
    onConnectWelinkOffice: async () => { throw new Error('Verification denied'); },
  });
  try {
    await view.click('welink-mail-connect');
    assert.match(view.find('welink-mail-error').textContent, /Verification denied/);
    assert.equal(view.find('welink-mail-connect').disabled, false);
  } finally { await view.close(); }
});

test('WeLink mail refreshes on reconnection and colleague changes, ignoring stale verification', async () => {
  const loads = [];
  let resolveConnect;
  const view = await mountProfile({
    initialSection: 'mail',
    onLoadWelinkOfficeStatus: async (id) => {
      loads.push(id);
      return { status: 'not_connected', email: `${id}@example.com` };
    },
    onConnectWelinkOffice: () => new Promise((resolve) => { resolveConnect = resolve; }),
  });
  try {
    await view.click('welink-mail-connect');
    assert.equal(view.find('welink-mail-connect').disabled, true);
    await view.rerender({ connectionEpoch: 1 });
    assert.deepEqual(loads, [colleague.id, colleague.id]);
    await view.rerender({ colleague: { ...colleague, id: 'second-agent' } });
    assert.equal(loads.at(-1), 'second-agent');
    await act(async () => resolveConnect({ status: 'connected', email: 'stale@example.com', account: 'stale' }));
    assert.equal(view.find('welink-mail-account'), null);
    assert.equal(view.find('welink-mail-email').value, 'second-agent@example.com');
  } finally { await view.close(); }
});

test('WeLink mail reports load and disconnect errors without hiding the connected account', async () => {
  const failed = await mountProfile({
    initialSection: 'mail',
    onLoadWelinkOfficeStatus: async () => { throw new Error('Status unavailable'); },
  });
  try {
    assert.match(failed.find('welink-mail-error').textContent, /Status unavailable/);
    assert.equal(failed.find('welink-mail-connect').disabled, true);
  } finally { await failed.close(); }
  const connected = await mountProfile({
    initialSection: 'mail',
    onLoadWelinkOfficeStatus: async () => ({ status: 'connected', email: 'employee@example.com', account: 'employee' }),
    onDisconnectWelinkOffice: async () => { throw new Error('Disconnect failed'); },
  });
  try {
    await connected.click('welink-mail-disconnect');
    assert.match(connected.find('welink-mail-error').textContent, /Disconnect failed/);
    assert.match(connected.find('welink-mail-account').textContent, /employee/);
    assert.equal(connected.find('welink-mail-disconnect').disabled, false);
  } finally { await connected.close(); }
});

test('clicking the selected reply mode does not save it again', async () => {
  const edits = [];
  const view = await mountProfile({ initialSection: 'channels', onSaveSettings: async (edit) => edits.push(edit) });
  try {
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-profile-group-reply-option"][data-variant="adaptive"]').click());
    assert.deepEqual(edits, []);
  } finally { await view.close(); }
});

test('reply mode errors roll back the selection and automatically clear', async () => {
  const view = await mountProfile({ initialSection: 'channels', onSaveSettings: async () => { throw new Error('Temporary connection failure'); } });
  const timeout = window.setTimeout;
  let dismiss;
  window.setTimeout = (callback, delay, ...args) => {
    if (delay === 5000) { dismiss = callback; return 5000; }
    return timeout(callback, delay, ...args);
  };
  try {
    await act(async () => view.container.querySelector('[data-testid="jiuwenmate-profile-group-reply-option"][data-variant="passive"]').click());
    assert.match(view.find('settings-error').textContent, /Temporary connection failure/);
    assert.equal(view.container.querySelector('[data-testid="jiuwenmate-profile-group-reply-option"][data-variant="adaptive"]').getAttribute('aria-pressed'), 'true');
    assert.equal(typeof dismiss, 'function');
    await act(async () => dismiss());
    assert.equal(view.find('settings-error'), null);
  } finally { window.setTimeout = timeout; await view.close(); }
});


test('removing an employee model drops the stale option and requires a valid replacement', async () => {
  const available = { id: 'kimi#1', label: 'Kimi', provider: 'OpenAI' };
  const view = await mountProfile({ initialSection: 'model', modelCatalog: {
    models: [{ id: 'deepseek#0', label: 'DeepSeek', provider: 'OpenAI' }, available],
    defaultModelId: 'deepseek#0', defaultModelLabel: 'DeepSeek',
  } });
  try {
    assert.ok(view.container.querySelector('input[value="deepseek#0"]:checked'));
    await view.rerender({ modelCatalog: { models: [available], defaultModelId: 'kimi#1', defaultModelLabel: 'Kimi' } });
    assert.ok(!view.container.querySelector('input[value="deepseek#0"]'));
    assert.doesNotMatch(view.container.textContent, /deepseek#0/);
    assert.ok(view.find('model-unavailable'));
    assert.equal(view.container.querySelector('input:checked'), null);
    assert.equal(view.container.querySelector('.jiuwenmate-profile__save-button').disabled, true);
    const notice = view.find('model-unavailable');
    await act(async () => view.container.querySelector('input[value="kimi#1"]').click());
    assert.ok(view.find('model-unavailable') === notice, 'Keep the notice and its layout space until the selection is saved');
    assert.equal(view.container.querySelector('.jiuwenmate-profile__save-button').disabled, false);
    await view.rerender({ colleague: { ...colleague, capabilities: { ...colleague.capabilities, model: 'kimi#1' } } });
    assert.ok(!view.find('model-unavailable'));
    await view.rerender({ modelCatalog: { models: [], defaultModelId: '', defaultModelLabel: '' } });
    assert.ok(view.find('model-empty'));
    assert.equal(view.container.querySelector('input[name="jiuwenmate-profile-model"]'), null);
    assert.equal(view.container.querySelector('.jiuwenmate-profile__save-button'), null);
  } finally { await view.close(); }
});

const dashboardPlan = {
  id: 'plan-run',
  agentId: 'agent-1',
  name: '晨报',
  prompt: '汇总昨日事项',
  cronExpr: '0 9 * * *',
  timezone: 'Asia/Shanghai',
  enabled: true,
  nextFireAt: '2026-10-18T01:00:00+00:00',
  scheduleKind: 'cron',
  intervalSeconds: null,
  runAt: null,
  startsAt: null,
  endsAt: null,
  conversationId: 'schedule:plan-run',
  runCount: 0,
  lastStatus: 'completed',
};

const dashboardWeeklyPlan = {
  ...dashboardPlan,
  id: 'plan-weekly',
  name: '周报',
  conversationId: 'schedule:plan-weekly',
};

const dashboardProps = {
  colleagues: [{
    id: 'agent-1',
    name: '小九',
    description: '通用办公助理',
    tags: [],
    category: 'custom',
    imConfigured: true,
    disabled: false,
  }],
  schedules: [dashboardPlan, dashboardWeeklyPlan],
  savingSchedule: false,
  activity: null,
  activityStatus: 'ready',
  onCreate: noop,
  onChat: noop,
  onManage: noop,
  onToggleDisabled: noop,
  onDelete: noop,
  onOpenTrace: noop,
  onCreateSchedule: noop,
  onUpdateSchedule: noop,
  onSetScheduleEnabled: noop,
  onDeleteSchedule: noop,
  onRunSchedule: noop,
  onLoadScheduleHistory: async () => ({ plan: dashboardPlan, runs: [] }),
};

async function mountDashboard(extra = {}) {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  dom.window.matchMedia = () => ({ matches: false, addEventListener() {}, removeEventListener() {} });
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  await act(async () => {
    root.render(React.createElement(OfficeDashboardPage, { ...dashboardProps, ...extra }));
  });
  return {
    container,
    find(selector) {
      return container.querySelector(selector);
    },
    async click(element) {
      assert.ok(element, 'Missing element to click');
      await act(async () => { element.click(); });
    },
    async close() {
      await act(async () => root.unmount());
      dom.window.close();
      if (previous.window === undefined) delete globalThis.window;
      else globalThis.window = previous.window;
      if (previous.document === undefined) delete globalThis.document;
      else globalThis.document = previous.document;
      if (previous.actEnvironment === undefined) delete globalThis.IS_REACT_ACT_ENVIRONMENT;
      else globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
    },
  };
}

for (const [language, message] of [
  ['zh', '请选择执行此任务的员工'],
  ['en', 'Select an AI employee to run this task.'],
]) {
  test(`a missing schedule executor shows a focused field error and clears after selection in ${language}`, async () => {
    await i18n.changeLanguage(language);
    const created = [];
    const view = await mountDashboard({ onCreateSchedule: (...args) => { created.push(args); } });
    try {
      await view.click(view.find('[data-testid="jiuwenmate-segmented-tab"][data-variant="tasks"]'));
      await view.click(view.find('.jiuwenmate-schedule-board .jiuwenmate-dark-button'));
      const executor = view.find('[data-testid="jiuwenmate-schedule-executor"]');
      assert.equal(executor.getAttribute('aria-invalid'), null);
      await view.click(view.find('[data-testid="jiuwenmate-schedule-save"]'));
      assert.deepEqual(created, []);
      const error = view.find('[data-testid="jiuwenmate-schedule-executor-error"]');
      assert.ok(error, 'The missing executor error must be attached to its field');
      assert.equal(error.textContent, message);
      assert.equal(error.getAttribute('role'), 'alert');
      assert.equal(error.parentElement, executor.parentElement);
      assert.equal(executor.getAttribute('aria-invalid'), 'true');
      assert.equal(executor.getAttribute('aria-describedby'), error.id);
      assert.equal(executor.ownerDocument.activeElement, executor);
      assert.equal(view.container.querySelectorAll('[role="alert"]').length, 1);
      await act(async () => {
        executor.value = 'agent-1';
        executor.dispatchEvent(new window.Event('change', { bubbles: true }));
      });
      assert.equal(view.find('[data-testid="jiuwenmate-schedule-executor-error"]'), null);
      assert.equal(executor.getAttribute('aria-invalid'), null);
      assert.equal(executor.getAttribute('aria-describedby'), null);
    } finally { await view.close(); await i18n.changeLanguage('zh'); }
  });
}

test('the task center runs the scheduled task whose menu was opened', async () => {
  const runs = [];
  const view = await mountDashboard({ onRunSchedule: (planId) => { runs.push(planId); } });
  try {
    await view.click(view.find('[data-testid="jiuwenmate-segmented-tab"][data-variant="tasks"]'));
    assert.ok(view.find('[data-testid="jiuwenmate-schedule-board"]'));
    // Locate the row by its task name, a locator that predates this fix, so a
    // failure points at the missing menu item rather than a testid added here.
    const row = [...view.container.querySelectorAll('tr')].find((item) => item.textContent.includes('周报'));
    assert.ok(row, 'Missing the second scheduled task row');
    await view.click(row.querySelector('.jiuwenmate-menu__trigger'));
    const item = view.find('[role="menu"] [role="menuitem"][data-variant="run"]');
    assert.ok(item, 'The task center does not offer run now');
    // The table scrolls horizontally, so an in-flow list would be clipped to the table height.
    assert.equal(view.find('[role="menu"]').style.position, 'fixed');
    assert.equal(item.textContent, '立即执行');
    await view.click(item);
    assert.deepEqual(runs, ['plan-weekly']);
  } finally { await view.close(); }
});

test('a floating schedule menu closes on resize without treating Window as a Node', async () => {
  const view = await mountDashboard();
  try {
    await view.click(view.find('[data-testid="jiuwenmate-segmented-tab"][data-variant="tasks"]'));
    const row = [...view.container.querySelectorAll('tr')].find((item) => item.textContent.includes('周报'));
    await view.click(row.querySelector('.jiuwenmate-menu__trigger'));
    assert.ok(view.find('[role="menu"]'));
    await act(async () => {
      window.dispatchEvent(new window.Event('resize'));
    });
    assert.equal(view.find('[role="menu"]'), null);
  } finally { await view.close(); }
});


async function changeScheduleField(view, selector, value) {
  const field = view.find(selector);
  assert.ok(field, `Missing schedule field ${selector}`);
  await act(async () => {
    Object.getOwnPropertyDescriptor(Object.getPrototypeOf(field), 'value').set.call(field, value);
    const Event = field.ownerDocument.defaultView.Event;
    field.dispatchEvent(new Event('input', { bubbles: true }));
    field.dispatchEvent(new Event('change', { bubbles: true }));
  });
}

for (const language of ['zh', 'en']) {
  test(`schedule required fields highlight and independently clear on correction in ${language}`, async () => {
    await i18n.changeLanguage(language);
    const created = [];
    const view = await mountDashboard({ onCreateSchedule: (...args) => { created.push(args); } });
    try {
      await view.click(view.find('[data-testid="jiuwenmate-segmented-tab"][data-variant="tasks"]'));
      await view.click(view.find('.jiuwenmate-schedule-board .jiuwenmate-dark-button'));
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-executor"]', 'agent-1');
      const name = view.find('[data-testid="jiuwenmate-schedule-name"]');
      const prompt = view.find('[data-testid="jiuwenmate-schedule-prompt"]');
      await view.click(view.find('[data-testid="jiuwenmate-schedule-save"]'));
      assert.equal(name.getAttribute('aria-invalid'), 'true');
      assert.equal(prompt.getAttribute('aria-invalid'), 'true');
      assert.equal(name.ownerDocument.activeElement, name);
      const nameError = name.ownerDocument.getElementById(name.getAttribute('aria-describedby'));
      const promptError = prompt.ownerDocument.getElementById(prompt.getAttribute('aria-describedby'));
      assert.equal(nameError.textContent, language === 'zh' ? '请输入任务名称' : 'Enter a task name.');
      assert.equal(promptError.textContent, language === 'zh' ? '请输入任务描述' : 'Enter a task description.');
      assert.equal(nameError.parentElement, name.parentElement);
      assert.equal(promptError.parentElement, prompt.parentElement);
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-name"]', '   ');
      assert.equal(name.getAttribute('aria-invalid'), 'true');
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-name"]', 'Drink water');
      assert.equal(name.getAttribute('aria-invalid'), null);
      assert.equal(prompt.getAttribute('aria-invalid'), 'true');
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-prompt"]', 'Take a short break');
      assert.equal(prompt.getAttribute('aria-invalid'), null);
      assert.equal(view.find('[data-testid="jiuwenmate-schedule-dialog"] [role="alert"]'), null);
      assert.deepEqual(created, []);
      await view.click(view.find('[data-testid="jiuwenmate-schedule-save"]'));
      assert.equal(created.length, 1);
      assert.equal(created[0][0], 'agent-1');
    } finally { await view.close(); await i18n.changeLanguage('zh'); }
  });

  test(`schedule effective range stays highlighted until a valid or open-ended range is entered in ${language}`, async () => {
    await i18n.changeLanguage(language);
    const created = [];
    const view = await mountDashboard({ onCreateSchedule: (...args) => { created.push(args); } });
    try {
      await view.click(view.find('[data-testid="jiuwenmate-segmented-tab"][data-variant="tasks"]'));
      await view.click(view.find('.jiuwenmate-schedule-board .jiuwenmate-dark-button'));
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-executor"]', 'agent-1');
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-name"]', 'Drink water');
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-prompt"]', 'Take a short break');
      const startSelector = 'input[aria-label="' + i18n.t('jiuwenmate.schedule.startsOn') + '"]';
      const endSelector = 'input[aria-label="' + i18n.t('jiuwenmate.schedule.endsOn') + '"]';
      const start = view.find(startSelector);
      const end = view.find(endSelector);
      await changeScheduleField(view, startSelector, '2030-02-02');
      await changeScheduleField(view, endSelector, '2030-02-01');
      await view.click(view.find('[data-testid="jiuwenmate-schedule-save"]'));
      assert.equal(start.getAttribute('aria-invalid'), 'true');
      assert.equal(end.getAttribute('aria-invalid'), 'true');
      assert.equal(end.ownerDocument.activeElement, end);
      const error = end.ownerDocument.getElementById(end.getAttribute('aria-describedby'));
      assert.equal(error.textContent, i18n.t('jiuwenmate.schedule.error.rangeInvalid'));
      await changeScheduleField(view, '[data-testid="jiuwenmate-schedule-name"]', 'Updated reminder');
      assert.equal(end.getAttribute('aria-invalid'), 'true', 'Unrelated edits must not hide an invalid range');
      await changeScheduleField(view, startSelector, '2030-01-31');
      assert.equal(start.getAttribute('aria-invalid'), null);
      assert.equal(end.getAttribute('aria-invalid'), null);
      assert.equal(view.find('[data-testid="jiuwenmate-schedule-dialog"] [role="alert"]'), null);
      await changeScheduleField(view, endSelector, '2029-12-31');
      assert.equal(end.getAttribute('aria-invalid'), 'true');
      await changeScheduleField(view, endSelector, '');
      assert.equal(end.getAttribute('aria-invalid'), null, 'An omitted optional end remains valid');
      await changeScheduleField(view, endSelector, '2030-01-31');
      assert.equal(end.getAttribute('aria-invalid'), null, 'The same calendar day ends at 23:59');
      assert.deepEqual(created, []);
      await view.click(view.find('[data-testid="jiuwenmate-schedule-save"]'));
      assert.equal(created.length, 1);
    } finally { await view.close(); await i18n.changeLanguage('zh'); }
  });
}

for (const [language, expected] of [['zh', '已过期'], ['en', 'Expired']]) {
  test(`finished schedule plans are expired and successful history records remain completed in ${language}`, async () => {
    await i18n.changeLanguage(language);
    const view = await mountDashboard({ schedules: [
      { ...dashboardPlan, id: 'once-done', name: 'One-off', scheduleKind: 'once', runCount: 1, enabled: false, nextFireAt: null },
      { ...dashboardPlan, id: 'period-done', name: 'Periodic', runCount: 1, enabled: false, nextFireAt: null },
      { ...dashboardPlan, id: 'interval-done', name: 'Interval', scheduleKind: 'interval', intervalSeconds: 60, runCount: 1, enabled: false, nextFireAt: null },
      { ...dashboardPlan, id: 'period-active', name: 'Active periodic', lastStatus: 'running', runCount: 1 },
      { ...dashboardPlan, id: 'interval-active', name: 'Active interval', scheduleKind: 'interval', intervalSeconds: 60, lastStatus: 'running', runCount: 1 },
    ], onLoadScheduleHistory: async () => ({ plan: dashboardPlan, runs: [
      { requestId: 'run-ok', scheduledFor: '2026-11-10T01:00:00Z', status: 'completed', description: '' },
      { requestId: 'run-failed', scheduledFor: '2026-11-09T01:00:00Z', status: 'failed', description: '' },
    ] }) });
    try {
      await view.click(view.find('[data-testid="jiuwenmate-segmented-tab"][data-variant="tasks"]'));
      const row = view.find('[data-testid="jiuwenmate-schedule-board-row"][data-variant="once-done"]');
      assert.equal(row.querySelector('.jiuwenmate-schedule-board__status').textContent, expected);
      for (const id of ['period-done', 'interval-done']) {
        assert.equal(view.find(`[data-variant="${id}"] .jiuwenmate-schedule-board__status`).textContent,
          expected);
      }
      for (const id of ['period-active', 'interval-active']) {
        assert.equal(view.find(`[data-variant="${id}"] .jiuwenmate-schedule-board__status`).textContent,
          language === 'zh' ? '运行中' : 'Running');
      }
      const status = view.find('select[aria-label="' + i18n.t('jiuwenmate.schedule.runStatus') + '"]');
      assert.ok(status.querySelector('option[value="expired"]'));
      assert.equal(status.querySelector('option[value="completed"]'), null);
      await act(async () => {
        status.value = 'expired';
        status.dispatchEvent(new window.Event('change', { bubbles: true }));
      });
      assert.deepEqual([...view.container.querySelectorAll('[data-testid="jiuwenmate-schedule-board-row"]')]
        .map(item => item.dataset.variant), ['once-done', 'period-done', 'interval-done']);
      await view.click(view.find('[data-variant="once-done"] .jiuwenmate-menu__trigger'));
      await view.click(view.find('[role="menuitem"][data-variant="history"]'));
      const history = view.find('[data-testid="jiuwenmate-schedule-history"]');
      assert.ok(history);
      assert.deepEqual([...history.querySelectorAll('.jiuwenmate-schedule-history__status')].map(item => item.textContent),
        language === 'zh' ? ['已完成', '运行失败'] : ['Completed', 'Failed']);
    } finally { await view.close(); await i18n.changeLanguage('zh'); }
  });
}
