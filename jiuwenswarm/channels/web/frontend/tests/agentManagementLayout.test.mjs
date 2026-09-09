import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

await i18next.use(initReactI18next).init({
  lng: 'zh',
  showSupportNotice: false,
  resources: {
    zh: {
      translation: {
        agentManagement: {
          title: '专家管理',
          subtitle: '创建并管理专家',
          tabsLabel: '专家管理分类',
          tabs: { catalog: '专家广场', mine: '我的专家' },
          searchLabel: '搜索专家',
          searchCatalog: '搜索专家',
          searchMine: '搜索我的专家',
          categories: { all: '全部' },
          states: { loading: '加载中' },
        },
      },
    },
  },
  interpolation: { escapeValue: false },
});

test('expert catalog renders inside the standard page shell and toolbar', async () => {
  const { AgentManagementPanel } =
    await import('../node_modules/.cache/agent-management-layout/AgentManagementPanel.mjs');

  const originalConsoleError = console.error;
  console.error = (...args) => {
    if (!String(args[0]).includes('useLayoutEffect does nothing on the server')) {
      originalConsoleError(...args);
    }
  };
  let markup;
  try {
    markup = renderToStaticMarkup(React.createElement(AgentManagementPanel));
  } finally {
    console.error = originalConsoleError;
  }

  assert.match(markup, /class="app-page-body"/);
  assert.match(
    markup,
    /class="page-content agent-management-panel agent-management-panel--catalog"[^>]*data-testid="agent-management-panel"/,
  );
  assert.match(markup, /data-testid="common-page-header"/);
  assert.match(markup, /class="page-toolbar"[^>]*data-testid="page-toolbar"/);
  assert.match(markup, /class="chat-picker-panel__tabs"[^>]*data-testid="agent-management-primary-tabs"/);
  assert.match(markup, /data-testid="agent-management-search"[^>]*class="relative flex-shrink-0"/);
});
