import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const panelSource = readFileSync(new URL('../src/components/AgentManagementPanel/index.tsx', import.meta.url), 'utf8');
const groupCatalogSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/GroupCatalogPage.tsx', import.meta.url),
  'utf8',
);
const groupCardSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/GroupCard.tsx', import.meta.url),
  'utf8',
);
const groupEditorSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/AgentGroupEditor.tsx', import.meta.url),
  'utf8',
);
const agentDetailSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/DefinitionDetailPage.tsx', import.meta.url),
  'utf8',
);
const groupDetailSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/AgentGroupDetailPage.tsx', import.meta.url),
  'utf8',
);
const groupUploadSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/AgentGroupUploadDialog.tsx', import.meta.url),
  'utf8',
);
const inputAreaSource = readFileSync(new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8');

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
  // 2026-09-11 页签迁移到共享 ui/Tabs：class 变为 "tabs ..."（role=tablist 不变），
  // tab 项语义由 data-testid="agent-management-primary-tab" + data-variant 表达
  assert.match(markup, /class="tabs[^"]*"[^>]*data-testid="agent-management-primary-tabs"/);
  assert.match(markup, /data-testid="agent-management-primary-tab"[^>]*data-variant="catalog"/);
  assert.match(markup, /data-testid="agent-management-primary-tab"[^>]*data-variant="mine"/);
  assert.match(markup, /data-testid="agent-management-search"[^>]*class="relative flex-shrink-0"/);
});

test('Expert and Expert Team management keep the shared page shell and field limits', () => {
  assert.match(panelSource, /<div className="page-shell flex-none"[^>]*>\s*<PageHeader/);
  assert.match(groupCatalogSource, /className="page-shell agent-management-toolbar"/);
  assert.match(groupCatalogSource, /className="page-scroll min-h-0 flex-1 overflow-y-auto"/);
  assert.match(groupCardSource, /<article[\s\S]*agent-group-card/);
  assert.match(groupCardSource, /data-testid="agent-group-card-open"/);
  assert.match(groupCardSource, /className="agent-management-card__actions"/);
  assert.doesNotMatch(groupCardSource, /<PageCard/);
  assert.match(groupEditorSource, /id="agent-management-group-name"[\s\S]*maxLength=\{AGENT_NAME_MAX_LENGTH\}/);
  assert.match(
    groupEditorSource,
    /id="agent-management-group-description"[\s\S]*maxLength=\{AGENT_DESCRIPTION_MAX_LENGTH\}/,
  );
  assert.doesNotMatch(groupEditorSource, /detail-back mb-\[35px\]/);
});

test('primary management tabs retain tab semantics and chat picker enforces mode locks', () => {
  assert.match(panelSource, /<Tabs[\s\S]*ariaLabel=\{t\('agentManagement\.tabsLabel'\)\}/);
  assert.match(panelSource, /\{ value: 'catalog', label: t\('agentManagement\.tabs\.catalog'\) \}/);
  assert.match(panelSource, /\{ value: 'teams', label: t\('agentManagement\.tabs\.teams'\) \}/);
  assert.match(panelSource, /\{ value: 'mine', label: t\('agentManagement\.tabs\.mine'\) \}/);
  assert.match(
    inputAreaSource,
    /const existingTeamGroupSelectionDisabled = Boolean\([\s\S]*activeSessionId !== NEW_CONVERSATION_ID/,
  );
  assert.match(
    inputAreaSource,
    /const agentSelectionDisabled = isTeamMode;/,
  );
  assert.match(
    inputAreaSource,
    /const agentGroupSelectionDisabled = isAgentMode \|\| agentGroupPickerLocked;/,
  );
  assert.match(
    inputAreaSource,
    /aria-disabled=\{agentSelectionDisabled\}[\s\S]*disabled=\{agentSelectionDisabled\}/,
  );
  assert.match(
    inputAreaSource,
    /aria-disabled=\{agentGroupSelectionDisabled\}[\s\S]*disabled=\{agentGroupSelectionDisabled\}/,
  );
  assert.match(inputAreaSource, /chat\.agentOnlyInSingleAgentMode/);
  assert.match(inputAreaSource, /chat\.agentGroupOnlyInTeamMode/);
  assert.match(inputAreaSource, /if \(!activeSessionId \|\| agentSelectionDisabled\) return;/);
  assert.match(inputAreaSource, /if \(agentGroupSelectionDisabled \|\| !activeSessionId\) return;/);
  assert.match(inputAreaSource, /agentSelectionDisabled && 'is-locked'/);
  assert.match(inputAreaSource, /agentGroupSelectionDisabled && 'is-locked'/);
});

test('Expert and Expert Team content details use the same full-width markdown layout', () => {
  assert.match(agentDetailSource, /<MarkdownPane[\s\S]*testId="agent-management-detail-content"/);
  assert.match(groupDetailSource, /<MarkdownPane[\s\S]*testId="agent-group-detail-content"/);
});

test('Expert Team leader badge does not add a redundant status icon', () => {
  assert.doesNotMatch(groupDetailSource, /import \{ Check \} from 'lucide-react';/);
  assert.match(
    groupDetailSource,
    /<span\s+className="agent-group-member-card__badge"[^>]*>\s*\{t\('agentManagement\.group\.detail\.leader'\)\}\s*<\/span>/,
  );
});

test('uninstalled local Expert Team details expose delete before install', () => {
  assert.match(groupDetailSource, /const canDelete = detail\.source === 'local' && !detail\.installed;/);
  const deleteAction = groupDetailSource.indexOf("t('agentManagement.actions.delete')");
  const installAction = groupDetailSource.indexOf("t('agentManagement.group.actions.install')");
  assert.ok(deleteAction >= 0 && installAction >= 0 && deleteAction < installAction);
  assert.match(groupDetailSource, /onClick=\{\(\) => onUninstall\(detail\.id\)\}/);
});

test('Expert Team upload dialog puts Expert first and removes the reference link', () => {
  const expertTab = groupUploadSource.indexOf("t('agentManagement.group.form.uploadAgentTab')");
  const groupTab = groupUploadSource.indexOf("t('agentManagement.group.form.uploadGroupTab')");
  assert.ok(expertTab >= 0 && groupTab >= 0 && expertTab < groupTab);
  assert.match(
    groupUploadSource,
    /const typeHint = kind === 'group' \? t\('agentManagement\.group\.form\.uploadHint'\) : t\('agentManagement\.form\.uploadHint'\)/,
  );
  assert.doesNotMatch(groupUploadSource, /uploadHintReference|hint-reference/);
});
