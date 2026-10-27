import assert from 'node:assert/strict';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { test } from 'node:test';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const pluginRoot = resolve('../../../extensions/video_duplex/frontend');
const outfile = resolve('node_modules/.cache/designer-duplex/test.mjs');
await build({
  stdin: { contents: `export { DesignerChatPanel } from './src/features/designer/components/DesignerChatPanel.tsx';
    export * from ${JSON.stringify(`${pluginRoot}/taskFullDuplexRuntimeStore.ts`)};`, resolveDir: process.cwd(), loader: 'tsx' },
  outfile, bundle: true, platform: 'node', format: 'esm', packages: 'external', jsx: 'automatic',
  loader: { '.css': 'empty' }, define: { 'import.meta.env.DEV': 'false' },
  plugins: [{ name: 'designer-duplex-ports', setup(builder) {
    builder.onResolve({ filter: /(?:designerStore|designerUiStore|designerEntry|stores\/chatStore|stores\/sessionStore|ApplicationPluginOutlet|services\/webClient|taskFullDuplex\/featureFlag|components\/form|components\/ModelProviderIcon|components\/ChatPanel\/(?:MessageList|InlineQuestionCard)|components\/InteractionSlot)$/ }, ({ path }) => ({ path, namespace: 'ports' }));
    builder.onResolve({ filter: /\.svg\?react$/ }, () => ({ path: 'svg', namespace: 'ports' }));
    builder.onLoad({ filter: /.*/, namespace: 'ports' }, ({ path }) => {
      let contents;
      if (path.endsWith('ApplicationPluginOutlet')) contents = `export { TaskFullDuplexAction as ApplicationPluginTaskInputActions } from ${JSON.stringify(`${pluginRoot}/TaskFullDuplexAction.tsx`)};`;
      else if (path.endsWith('designerStore')) contents = 'export const useDesignerStore = selector => selector(globalThis.designerDuplex.state); useDesignerStore.getState = () => globalThis.designerDuplex.state;';
      else if (path.endsWith('designerUiStore')) contents = 'export const useDesignerUiStore = selector => selector({toggleChatSidebar() {}});';
      else if (path.endsWith('designerEntry')) contents = 'export const DESIGNER_TEAM_MODE = "team.design"; export const bindDesignerChatSession = id => globalThis.designerDuplex.bindings.push(id); export const composeDesignerSession = async () => {}; export const sendDesignerLeaderMessage = async () => {};';
      else if (path.endsWith('stores/chatStore')) contents = 'const chatState = {runtimes: {}, activeSessionId: "design-chat"}; export const useChatStore = Object.assign(selector => selector(chatState), {getState: () => chatState, subscribe: () => () => {}});';
      else if (path.endsWith('stores/sessionStore')) contents = 'export const useSessionStore = selector => selector({runtimes: {}, chatAvailableModels: [], defaultModelName: null}); export const resolveChatModelSelection = () => null;';
      else if (path.endsWith('services/webClient')) contents = 'export const webClient = {request: async () => ({})}; export const webRequest = async () => ({task_full_duplex_enabled: true});';
      else if (path.endsWith('featureFlag')) contents = 'export const useTaskFullDuplexEnabled = () => true; export const setTaskFullDuplexEnabled = () => {}; export const setTaskAsrEnabled = () => {};';
      else if (path.endsWith('components/form')) contents = 'export const FormDialog = () => null;';
      else if (path.endsWith('ModelProviderIcon')) contents = 'export const ModelProviderIcon = () => null;';
      else if (path.endsWith('MessageList')) contents = 'export const MessageList = () => null;';
      else if (path.endsWith('InlineQuestionCard')) contents = 'export const InlineQuestionCard = () => null;';
      else if (path.endsWith('InteractionSlot')) contents = 'export const InteractionSlot = () => null;';
      else contents = 'export default () => null;';
      return { contents, loader: 'tsx', resolveDir: process.cwd() };
    });
  } }],
});
const { DesignerChatPanel, registerTaskFullDuplexController, stopTaskFullDuplex } = await import(pathToFileURL(outfile));

test('existing Design project starts shared duplex only after its graph is saved', async () => {
  const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
  const previous = new Map();
  for (const [name, value] of Object.entries({ window: dom.window, document: dom.window.document, IS_REACT_ACT_ENVIRONMENT: true })) {
    previous.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  const starts = [];
  let finishSave;
  globalThis.designerDuplex = { bindings: [], state: {
    domainGraph: { graph_id: 'graph', project_id: 'project', metadata: { session_id: 'design-chat' }, nodes: [], edges: [] },
    flushSave: () => new Promise(resolve => { finishSave = resolve; }),
  } };
  registerTaskFullDuplexController({ async startScreenDuplex(id) { starts.push(id); return true; }, stop() {} }, async id => `task-duplex:${id}`);
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () => root.render(createElement(DesignerChatPanel)));
    const button = document.querySelector('[data-testid="chat-panel-input-full-duplex"]');
    assert.ok(button, 'Design composer reuses the existing duplex action');
    await act(async () => button.click());
    assert.equal(typeof finishSave, 'function');
    assert.deepEqual(starts, []);
    await act(async () => finishSave());
    assert.deepEqual(starts, ['task-duplex:design-chat']);
    await act(async () => stopTaskFullDuplex());
    globalThis.designerDuplex.state.flushSave = async () => { throw new Error('Graph save failed'); };
    await act(async () => document.querySelector('[data-testid="chat-panel-input-full-duplex"]').click());
    assert.deepEqual(starts, ['task-duplex:design-chat'], 'saving failure must not start media');
    assert.ok(document.body.textContent.includes('Graph save failed'));
    await act(async () => {
      stopTaskFullDuplex();
      globalThis.designerDuplex.state.domainGraph = null;
      root.render(createElement(DesignerChatPanel));
    });
    assert.equal(document.querySelector('[data-testid="chat-panel-input-full-duplex"]'), null, 'an unbound Design landing cannot start a voice run');
  } finally {
    await act(async () => { stopTaskFullDuplex(); root.unmount(); });
    registerTaskFullDuplexController(null);
    dom.window.close();
    delete globalThis.designerDuplex;
    for (const [name, descriptor] of previous) {
      if (descriptor) Object.defineProperty(globalThis, name, descriptor);
      else delete globalThis[name];
    }
  }
});
