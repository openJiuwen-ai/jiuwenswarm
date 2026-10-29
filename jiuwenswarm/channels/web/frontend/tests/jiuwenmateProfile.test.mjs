import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';
import { renderToStaticMarkup } from 'react-dom/server';
import { initReactI18next } from 'react-i18next';

import { ColleagueProfilePage } from '../node_modules/.cache/jiuwenmate-profile/pages/ColleagueProfilePage.js';
import { ScheduleBoard } from '../node_modules/.cache/jiuwenmate-profile/schedules/ScheduleBoard.js';
import { ColleagueDeleteDialog } from '../node_modules/.cache/jiuwenmate-profile/components/ColleagueDeleteDialog.js';

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const profileStyles = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');
await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false } });

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
  const timers = new Map();
  let timerId = 0;
  dom.window.setInterval = (callback) => { timers.set(++timerId, callback); return timerId; };
  dom.window.clearInterval = (id) => timers.delete(id);
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  await act(async () => {
    root.render(React.createElement(ColleagueProfilePage, {
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
    }));
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  const find = (id) => container.querySelector(`[data-testid="jiuwenmate-profile-${id}"]`);
  return {
    container,
    find,
    async click(id) {
      assert.ok(find(id), `Missing profile ${id}`);
      await act(async () => { find(id).click(); await new Promise((resolve) => setTimeout(resolve, 0)); });
    },
    async flush() { await act(async () => { await new Promise((resolve) => setTimeout(resolve, 0)); }); },
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

test('the overview hides quick IM setup after the channel is configured', () => {
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

  const overview = renderProfile({ colleague: authorized });
  assert.doesNotMatch(overview, /data-testid="jiuwenmate-profile-quick-im"/);
});

test('the employee mail page gives a short connection note', () => {
  const markup = renderProfile({ initialSection: 'mail' });
  assert.match(markup, /data-variant="mail"/);
  assert.match(markup, /腾讯 Agent Mail/);
  assert.match(markup, /为该员工单独连接腾讯邮箱，授权后即可在新对话中使用。/);
  assert.doesNotMatch(markup, /OAuth/);
  assert.doesNotMatch(markup, /无需安装 Docker/);
  assert.doesNotMatch(markup, /运行镜像/);
  assert.match(markup, /data-testid="jiuwenmate-profile-mail-setup"/);
  assert.match(markup, /正在检查邮箱连接/);
  assert.match(markup, /<button[^>]*disabled=""[^>]*data-testid="jiuwenmate-profile-mail-start"/);
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
    assert.match(popup.document.body.textContent, /正在准备腾讯邮箱授权/);
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
  assert.match(markup, /左侧「知识」/);
});

test('channel reply mode renders a compact selected state and disables changes while saving', () => {
  const html = renderProfile({ initialSection: 'channels', colleague: { ...colleague, groupReplyMode: 'passive' } });
  assert.match(html, /aria-pressed="true"[^>]*data-variant="passive"/);
  assert.match(html, /aria-pressed="false"[^>]*data-variant="adaptive"/);
  assert.match(html, /钉钉群里只有 @ 到机器人的消息会触发回复/);
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
