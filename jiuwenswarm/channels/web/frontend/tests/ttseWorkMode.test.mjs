import assert from 'node:assert/strict';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import React, { act, createRef } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost/' });
Object.assign(globalThis, {
  window: dom.window,
  document: dom.window.document,
  HTMLElement: dom.window.HTMLElement,
  Node: dom.window.Node,
  localStorage: dom.window.localStorage,
  sessionStorage: dom.window.sessionStorage,
  IS_REACT_ACT_ENVIRONMENT: true,
});
const { createRoot } = await import('react-dom/client');
const base = '../node_modules/.cache/ttse-work-mode/';
const { ExtensionPickerPanel } = await import(`${base}components/ChatPanel/ExtensionPickerPanel.mjs`);
const { useSessionStore } = await import(`${base}stores/sessionStore.mjs`);
const { useChatStore } = await import(`${base}stores/chatStore.mjs`);
const { useWorkspaceStore } = await import(`${base}stores/workspaceStore.mjs`);
const { usePluginPackageStore } = await import(`${base}stores/pluginPackageStore.mjs`);
const { useConnectorStore } = await import(`${base}stores/connectorStore.mjs`);
const { toastStore } = await import(`${base}components/ui/Toast/toastStore.mjs`);
const { default: i18n } = await import(`${base}i18n/index.mjs`);
const { restoreSessionEquipment, buildExtensionSendPayload } = await import(`${base}utils/enabledExtensions.mjs`);

function setup(mode = 'work') {
  useWorkspaceStore.setState({ workMode: mode });
  useChatStore.setState({ activeSessionId: 'ttse-session' });
  usePluginPackageStore.setState({
    localPackages: [
      { id: 'hub-asset-42', runtimePackageName: 'ttse', displayName: { zh: '任意名称', en: 'Renamed plugin' } },
      { id: 'ordinary', runtimePackageName: 'ordinary', displayName: { zh: '普通插件', en: 'Ordinary' } },
    ].map(pkg => ({ ...pkg, displayDescription: { zh: '', en: '' }, installed: true, source: 'local' })),
    installed: { 'hub-asset-42': true, ordinary: true },
    connectionStateMap: { 'hub-asset-42': 'connected', ordinary: 'connected' },
    isLoading: false,
    loadList: async () => {},
  });
  useConnectorStore.setState({
    connectors: [{ name: 'filesystem', connectionState: 'connected' }],
    myConnectors: [{ name: 'filesystem', displayName: 'Filesystem', connectionState: 'connected' }],
    isLoading: false,
    loadList: async () => {},
  });
  restoreSessionEquipment('ttse-session', { plugin_names: [], mcp: [] });
}

async function mount() {
  const root = createRoot(document.getElementById('root'));
  await act(async () => root.render(React.createElement(ExtensionPickerPanel, {
    onClose: () => {}, panelRef: createRef(),
  })));
  return root;
}

function pluginSwitch(id) {
  return document.querySelector(`[data-testid="chat-panel-extension-picker-item"][data-variant="${id}"] [role="switch"]`);
}

async function click(element) {
  assert.ok(element);
  await act(async () => element.dispatchEvent(new dom.window.MouseEvent('click', { bubbles: true })));
}

test('work click keeps renamed TTSE off and shows one six-second translated notice with bold setting', async () => {
  setup();
  await i18n.changeLanguage('zh');
  const root = await mount();
  try {
    await click(pluginSwitch('hub-asset-42'));
    await click(pluginSwitch('hub-asset-42'));
    assert.equal(pluginSwitch('hub-asset-42').getAttribute('aria-checked'), 'false');
    assert.deepEqual(useSessionStore.getState().runtimes['ttse-session'].enabledPlugins, []);
    const notices = toastStore.getSnapshot().filter(item => item.id === 'ttse-work-mode-notice');
    assert.equal(notices.length, 1);
    const notice = notices[0];
    assert.equal(notice.durationMs, 6000);
    assert.equal(notice.closable, true);
    assert.equal(notice.variant, 'info');
    assert.equal(notice.testId, 'chat-panel-ttse-work-mode-notice');
    const content = document.createElement('div');
    content.innerHTML = renderToStaticMarkup(notice.content);
    assert.equal(content.textContent, 'ttse经验沉淀功能已内置在work模式中，如需开启，请在设置中打开“本地环境事实与任务经验自动演进”开关。');
    assert.equal(content.querySelector('strong').textContent, '本地环境事实与任务经验自动演进');
    await act(async () => i18n.changeLanguage('en'));
    assert.match(renderToStaticMarkup(notice.content), /<strong[^>]*>Local environment facts and task experience auto-evolution<\/strong>/);
    await click(pluginSwitch('ordinary'));
    assert.deepEqual(buildExtensionSendPayload('ttse-session').plugin_names, ['ordinary']);
    await click(document.querySelector('[data-testid="chat-panel-extension-picker-tab"][data-variant="mcp"]'));
    await click(document.querySelector('[data-variant="filesystem"] [role="switch"]'));
    assert.deepEqual(buildExtensionSendPayload('ttse-session').mcp, ['filesystem']);
  } finally {
    await act(async () => root.unmount());
  }
});

test('code can toggle TTSE; an open panel clears its selection when switching to work', async () => {
  setup('code');
  const root = await mount();
  try {
    await click(pluginSwitch('hub-asset-42'));
    assert.equal(pluginSwitch('hub-asset-42').getAttribute('aria-checked'), 'true');
    assert.deepEqual(buildExtensionSendPayload('ttse-session').plugin_names, ['ttse']);
    await click(pluginSwitch('hub-asset-42'));
    assert.deepEqual(buildExtensionSendPayload('ttse-session').plugin_names, []);
    await click(pluginSwitch('hub-asset-42'));
    await act(async () => useWorkspaceStore.setState({ workMode: 'work' }));
    assert.equal(pluginSwitch('hub-asset-42').getAttribute('aria-checked'), 'false');
    assert.deepEqual(useSessionStore.getState().runtimes['ttse-session'].enabledPlugins, []);
  } finally {
    await act(async () => root.unmount());
  }
});

test('restore and send filter TTSE without catalog metadata and preserve hydration semantics', () => {
  setup();
  usePluginPackageStore.setState({ installed: {}, connectionStateMap: {} });
  assert.deepEqual(buildExtensionSendPayload('never-hydrated'), {});
  restoreSessionEquipment('ttse-session', { plugin_names: ['ttse', 'ordinary'], mcp: ['filesystem'] });
  assert.deepEqual(useSessionStore.getState().runtimes['ttse-session'].enabledPlugins, ['ordinary']);
  assert.deepEqual(buildExtensionSendPayload('ttse-session'), { plugin_names: ['ordinary'], mcp: ['filesystem'] });
  restoreSessionEquipment('ttse-session', { plugin_names: ['ttse'], mcp: [] });
  assert.deepEqual(buildExtensionSendPayload('ttse-session'), { plugin_names: [], mcp: [] });
  useWorkspaceStore.setState({ workMode: 'code' });
  restoreSessionEquipment('ttse-session', { plugin_names: ['ttse'], mcp: [] });
  assert.deepEqual(buildExtensionSendPayload('ttse-session').plugin_names, ['ttse']);
  useWorkspaceStore.setState({ workMode: 'work' });
  assert.deepEqual(buildExtensionSendPayload('ttse-session').plugin_names, []);
  assert.deepEqual(useSessionStore.getState().runtimes['ttse-session'].enabledPlugins, []);
});

test('opening the work picker clears stale selection; changing mode clears it with the picker closed', async () => {
  setup();
  useSessionStore.getState().restoreSessionEquipment('ttse-session', { plugin_names: ['ttse'], mcp: [] });
  const root = await mount();
  try {
    assert.equal(pluginSwitch('hub-asset-42').getAttribute('aria-checked'), 'false');
    assert.deepEqual(useSessionStore.getState().runtimes['ttse-session'].enabledPlugins, []);
  } finally {
    await act(async () => root.unmount());
  }
  useWorkspaceStore.setState({ workMode: 'code' });
  restoreSessionEquipment('ttse-session', { plugin_names: ['ttse', 'ordinary'], mcp: ['filesystem'] });
  const originalLoad = useWorkspaceStore.getState().loadProjects;
  useWorkspaceStore.setState({ loadProjects: async () => {} });
  try {
    await useWorkspaceStore.getState().setWorkMode('work');
    assert.deepEqual(useSessionStore.getState().runtimes['ttse-session'].enabledPlugins, ['ordinary']);
    assert.deepEqual(buildExtensionSendPayload('ttse-session'), { plugin_names: ['ordinary'], mcp: ['filesystem'] });
  } finally {
    useWorkspaceStore.setState({ loadProjects: originalLoad });
  }
});
