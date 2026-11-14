import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { after, test } from 'node:test';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import i18next from 'i18next';
import { I18nextProvider } from 'react-i18next';
import { A2UIProvider } from '@a2ui/react';
import { JSDOM, VirtualConsole } from 'jsdom';
import { createServer } from 'vite';
import svgr from 'vite-plugin-svgr';

const virtualConsole = new VirtualConsole();
virtualConsole.on('jsdomError', error => {
  if (error.type !== 'css parsing') throw error;
});
const dom = new JSDOM('<!doctype html><div id="root"></div>', {
  url: 'http://localhost/', pretendToBeVisual: true, virtualConsole,
});
const descriptors = new Map();
for (const [name, value] of Object.entries({
  window: dom.window, document: dom.window.document, localStorage: dom.window.localStorage,
  navigator: dom.window.navigator, Node: dom.window.Node, HTMLElement: dom.window.HTMLElement,
  Element: dom.window.Element, CustomEvent: dom.window.CustomEvent,
  MutationObserver: dom.window.MutationObserver, IS_REACT_ACT_ENVIRONMENT: true,
})) {
  descriptors.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
  Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
}
const originalFetch = globalThis.fetch;
after(() => {
  globalThis.fetch = originalFetch;
  dom.window.close();
  for (const [name, descriptor] of descriptors) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
});

const vite = await createServer({
  configFile: false,
  plugins: [svgr()],
  resolve: { dedupe: ['react', 'react-dom', 'react-i18next', 'i18next', 'zustand'] },
  cacheDir: 'node_modules/.cache/file-download-card/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: { 'import.meta.env.DEV': false },
});
let FileDownloadList;
let MessageItem;
let useChatStore;
let webClient;
try {
  ({ FileDownloadList, MessageItem } = await vite.ssrLoadModule('/src/components/ChatPanel/MessageItem.tsx'));
  ({ useChatStore } = await vite.ssrLoadModule('/src/stores/chatStore.ts'));
  ({ webClient } = await vite.ssrLoadModule('/src/services/webClient.ts'));
} finally { await vite.close(); }
const i18n = i18next.createInstance();
await i18n.init({
  lng: 'zh', initImmediate: false, showSupportNotice: false,
  interpolation: { escapeValue: false },
  resources: Object.fromEntries(['zh', 'en'].map(language => [language, {
    translation: JSON.parse(readFileSync(new URL(`../src/i18n/locales/${language}.json`, import.meta.url), 'utf8')),
  }])),
});

async function withCard(edition, status, run, file) {
  window.__JIUWENSWARM_EDITION__ = edition;
  let currentStatus = status;
  let downloads = 0;
  const setTimeout = globalThis.setTimeout;
  const timeouts = new Set();
  globalThis.setTimeout = (callback, delay, ...args) => {
    const timeout = setTimeout(callback, delay, ...args);
    timeouts.add(timeout);
    return timeout;
  };
  const anchorClick = dom.window.HTMLAnchorElement.prototype.click;
  dom.window.HTMLAnchorElement.prototype.click = () => { downloads += 1; };
  globalThis.fetch = async () => {
    if (currentStatus === 'network') throw new TypeError('Failed to fetch');
    return new Response(null, { status: currentStatus });
  };
  const root = createRoot(document.getElementById('root'));
  const files = [file || { name: '报告.docx', download_url: '/file-api/download?url=signed', size: 42 }];
  const render = () => act(async () => root.render(createElement(I18nextProvider, { i18n },
    createElement(FileDownloadList, { files: [...files] }))));
  const byId = id => document.querySelector(`[data-testid="${id}"]`);
  try {
    await render();
    await run({ byId, render, setStatus: value => { currentStatus = value; }, downloads: () => downloads });
  } finally {
    await act(async () => root.unmount());
    for (const timeout of timeouts) clearTimeout(timeout);
    globalThis.setTimeout = setTimeout;
    dom.window.HTMLAnchorElement.prototype.click = anchorClick;
    delete window.__JIUWENSWARM_EDITION__;
  }
}

const skillFile = { name: 'review.skill', download_token: 'review-token', download_url: '/file-api/download?token=review-token' };

for (const language of ['en', 'zh']) {
  test(`${language} message labels follow UI language and preserve message content`, async () => {
    const root = createRoot(document.getElementById('root'));
    const byId = id => document.querySelector(`[data-testid="${id}"]`);
    const render = message => act(async () => root.render(createElement(I18nextProvider, { i18n },
      createElement(A2UIProvider, null, createElement(MessageItem, { message, showAvatar: false, hideMeta: true })))));
    const sessionMessage = description => ({
      id: 'session-result-i18n', role: 'system', timestamp: 0,
      content: `chat.session_result:${JSON.stringify({ description, result: '原始结果 / original result' })}`,
    });
    const taskTitle = (locale, name) => locale === 'en'
      ? `Session task: 【${name}】 completed` : `会话任务：【${name}】已完成`;
    try {
      await i18n.changeLanguage(language);
      await render(sessionMessage('原始任务 / original task'));
      assert.equal(byId('chat-panel-session-result-card-title').textContent.trim(),
        taskTitle(language, '原始任务 / original task'));
      await act(async () => byId('chat-panel-session-result-card-header').click());
      assert.equal(byId('chat-panel-session-description-label').textContent.trim(), language === 'en' ? 'Description:' : '描述:');
      assert.equal(byId('chat-panel-session-result-label').textContent.trim(), language === 'en' ? 'Result:' : '结果:');
      assert.ok(byId('chat-panel-session-result-card-result').textContent.includes('原始结果 / original result'));
      await render(sessionMessage(''));
      assert.equal(byId('chat-panel-session-result-card-title').textContent.trim(),
        taskTitle(language, language === 'en' ? 'Unknown task' : '未知任务'));
      const nextLanguage = language === 'en' ? 'zh' : 'en';
      await act(async () => i18n.changeLanguage(nextLanguage));
      assert.equal(byId('chat-panel-session-result-card-title').textContent.trim(),
        taskTitle(nextLanguage, nextLanguage === 'en' ? 'Unknown task' : '未知任务'));
      await act(async () => i18n.changeLanguage(language));
      for (const id of ['team-leader-i18n', 'team-member-reviewer@i18n']) {
        await render({ id, role: 'system', content: '', timestamp: 0, isStreaming: true });
        assert.ok(byId('chat-panel-team-member-message').textContent.includes(language === 'en' ? 'Receiving...' : '正在接收中...'));
      }
      await render({ id: 'tool-result-i18n', role: 'assistant', content: 'Raw output', presentation: 'tool_result', timestamp: 0 });
      assert.equal(byId('chat-panel-tool-result-label').textContent.trim(),
        `${language === 'en' ? 'Tool result' : '工具结果'} · Jiuwen Core Agent`);
    } finally {
      await act(async () => root.unmount());
      await i18n.changeLanguage('zh');
    }
  });
}

const skillLabels = {
  en: {
    save: 'Save', saving: 'Saving...', saved: 'Saved', success: 'Saved successfully',
    noSession: 'No active conversation. The Skill cannot be saved.', overwrite: 'Overwrite the existing Skill?',
  },
  zh: {
    save: '保存', saving: '保存中...', saved: '已保存', success: '保存成功',
    noSession: '当前无活跃会话，无法保存 Skill', overwrite: '是否覆盖保存？',
  },
};

for (const [language, labels] of Object.entries(skillLabels)) {
  test(`${language} skill card localizes saving, success and live language changes`, async () => {
    const previousSessionId = useChatStore.getState().activeSessionId;
    const request = webClient.request;
    const savedTokens = localStorage.getItem('saved_skill_tokens');
    localStorage.removeItem('saved_skill_tokens');
    useChatStore.setState({ activeSessionId: 'skill-save-test' });
    let completeSave;
    webClient.request = async (method, params) => {
      assert.equal(method, 'skills.import_local');
      assert.equal(params.session_id, 'skill-save-test');
      return new Promise(resolve => { completeSave = resolve; });
    };
    try {
      await i18n.changeLanguage(language);
      await withCard('enterprise', 200, async ({ byId }) => {
        const button = byId('chat-panel-skill-save-btn');
        assert.equal(button.textContent, labels.save);
        assert.equal(button.title, labels.save);
        await act(async () => button.click());
        assert.equal(button.textContent, labels.saving);
        assert.equal(button.disabled, true);
        await act(async () => completeSave({}));
        assert.equal(button.textContent, labels.saved);
        assert.equal(button.title, labels.saved);
        assert.equal(button.disabled, true);
        assert.equal(byId('chat-panel-skill-save-success').textContent, labels.success);
        const nextLanguage = language === 'en' ? 'zh' : 'en';
        await act(async () => i18n.changeLanguage(nextLanguage));
        assert.equal(button.textContent, skillLabels[nextLanguage].saved);
        assert.equal(button.title, skillLabels[nextLanguage].saved);
        assert.equal(byId('chat-panel-skill-save-success').textContent, skillLabels[nextLanguage].success);
      }, skillFile);
    } finally {
      webClient.request = request;
      useChatStore.setState({ activeSessionId: previousSessionId });
      if (savedTokens === null) localStorage.removeItem('saved_skill_tokens');
      else localStorage.setItem('saved_skill_tokens', savedTokens);
      await i18n.changeLanguage('zh');
    }
  });

  test(`${language} skill card localizes the missing-session alert and overwrite confirmation`, async () => {
    const previousSessionId = useChatStore.getState().activeSessionId;
    const request = webClient.request;
    const alert = window.alert;
    const confirm = window.confirm;
    const savedTokens = localStorage.getItem('saved_skill_tokens');
    const alerts = [];
    const confirmations = [];
    const requests = [];
    localStorage.removeItem('saved_skill_tokens');
    useChatStore.setState({ activeSessionId: null });
    window.alert = message => alerts.push(message);
    window.confirm = message => { confirmations.push(message); return true; };
    webClient.request = async (method, params) => {
      requests.push({ method, params });
      if (!params.force) throw Object.assign(new Error('Existing skill'), { code: 'SKILL_ALREADY_EXISTS' });
      return {};
    };
    try {
      await i18n.changeLanguage(language);
      await withCard('enterprise', 200, async ({ byId }) => {
        await act(async () => byId('chat-panel-skill-save-btn').click());
        assert.deepEqual(alerts, [labels.noSession]);
        assert.equal(requests.length, 0);
        await act(async () => useChatStore.setState({ activeSessionId: 'skill-overwrite-test' }));
        await act(async () => byId('chat-panel-skill-save-btn').click());
        assert.deepEqual(confirmations, [`Existing skill\n${labels.overwrite}`]);
        assert.deepEqual(requests.map(({ params }) => params.force), [false, true]);
        assert.equal(byId('chat-panel-skill-save-btn').textContent, labels.saved);
      }, skillFile);
    } finally {
      webClient.request = request;
      window.alert = alert;
      window.confirm = confirm;
      useChatStore.setState({ activeSessionId: previousSessionId });
      if (savedTokens === null) localStorage.removeItem('saved_skill_tokens');
      else localStorage.setItem('saved_skill_tokens', savedTokens);
      await i18n.changeLanguage('zh');
    }
  });
}

for (const edition of ['enterprise', 'personal']) {
  for (const status of [200, 403, 404, 410, 405, 500, 503, 'network']) {
    test(`${edition} file card handles HEAD ${status}`, async () => {
      await withCard(edition, status, async ({ byId, setStatus, downloads }) => {
        const expired = status !== 200 && (edition === 'personal' || [403, 404, 410].includes(status));
        assert.equal(Boolean(byId('chat-panel-file-download-expired')), expired);
        const button = byId('chat-panel-file-download-btn');
        assert.equal(button.disabled, expired);
        setStatus(200);
        await act(async () => button.click());
        assert.equal(downloads(), expired ? 0 : 1);
      });
    });
  }
  test(`${edition} file card retains its expected recovery behavior`, async () => {
    await withCard(edition, 403, async ({ byId, setStatus, render }) => {
      assert.ok(byId('chat-panel-file-download-expired'));
      setStatus(200);
      await render();
      assert.equal(Boolean(byId('chat-panel-file-download-expired')), edition === 'personal');
    });
  });
}
