import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
after(() => dom.window.close());
const { createRoot } = await import('react-dom/client');
await i18next.use(initReactI18next).init({
  lng: 'en', showSupportNotice: false, resources: { en: { translation: {} } },
});
const { DefinitionDetailPage } = await import(
  '../node_modules/.cache/agent-definition-detail/components/AgentManagementPanel/DefinitionDetailPage.js'
);

const localDetail = {
  id: 'writer-expert',
  runtimePackageName: 'writer-expert',
  displayName: 'Writer Expert',
  description: 'drafts documents',
  category: 'writing',
  source: 'local',
  installed: true,
  connectionState: 'connected',
  enabled: true,
  tags: [],
  avatarUrl: null,
  prompt: '',
  details: '',
  persona: '',
  skills: [],
  tools: [],
  rails: [],
  mcps: [],
  suggestedPrompts: [],
  pendingConnectors: [],
};

function makeProps(detail, overrides = {}) {
  const calls = { edit: [], uninstall: [], delete: [] };
  const props = {
    detail,
    detailStatus: 'success',
    detailError: null,
    detailTab: 'content',
    files: [],
    filesStatus: 'idle',
    filesError: null,
    selectedFilePath: null,
    fileContent: null,
    fileStatus: 'idle',
    fileError: null,
    actionNotice: null,
    actionNoticeError: false,
    busy: false,
    onBack: () => {},
    onRetry: () => {},
    onTabChange: () => {},
    onRetryFiles: () => {},
    onSelectFile: () => {},
    onUse: () => {},
    onUsePrompt: () => {},
    onReconnect: () => {},
    onInstall: () => {},
    onUninstall: (id) => calls.uninstall.push(id),
    onDelete: (id, name) => calls.delete.push([id, name]),
    onEdit: (id) => calls.edit.push(id),
    ...overrides,
  };
  return { props, calls };
}

function renderDetailPage(props) {
  const root = createRoot(document.getElementById('root'));
  const rendered = { root };
  rendered.render = async () => {
    await act(async () => {
      root.render(React.createElement(DefinitionDetailPage, props));
    });
  };
  rendered.unmount = async () => {
    await act(async () => {
      root.unmount();
    });
  };
  return rendered;
}

test('local expert detail exposes an edit action that reports its id', async () => {
  const { props, calls } = makeProps(localDetail);
  const page = renderDetailPage(props);
  try {
    await page.render();
    const editBtn = document.querySelector('[data-testid="agent-management-detail-edit-btn"]');
    assert.ok(editBtn, '本地专家详情页应有编辑入口（props.onEdit 已传入却被丢弃时按钮缺失）');
    assert.equal(editBtn.disabled, false);
    await act(async () => {
      editBtn.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true }));
    });
    assert.deepEqual(calls.edit, ['writer-expert']);
    assert.deepEqual(calls.uninstall, [], '点击编辑不得触发卸载/删除');
    assert.deepEqual(calls.delete, []);
  } finally {
    await page.unmount();
  }
});

test('marketplace expert detail keeps no edit action', async () => {
  const hubDetail = { ...localDetail, id: 'hub-expert', source: 'hub' };
  const { props } = makeProps(hubDetail);
  const page = renderDetailPage(props);
  try {
    await page.render();
    assert.equal(document.querySelector('[data-testid="agent-management-detail-edit-btn"]'), null);
  } finally {
    await page.unmount();
  }
});

test('edit action is disabled while an action is busy', async () => {
  const { props } = makeProps(localDetail, { busy: true });
  const page = renderDetailPage(props);
  try {
    await page.render();
    const editBtn = document.querySelector('[data-testid="agent-management-detail-edit-btn"]');
    assert.ok(editBtn);
    assert.equal(editBtn.disabled, true);
  } finally {
    await page.unmount();
  }
});
