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
const catalogPageSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/CatalogPage.tsx', import.meta.url),
  'utf8',
);
const groupCardSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/GroupCard.tsx', import.meta.url),
  'utf8',
);
const agentManagementCss = readFileSync(
  new URL('../src/components/AgentManagementPanel/agentManagement.css', import.meta.url),
  'utf8',
);
const entityHeaderCss = readFileSync(
  new URL('../src/components/ui/EntityHeader/EntityHeader.css', import.meta.url),
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
const memberPickerSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/AgentGroupMemberPicker.tsx', import.meta.url),
  'utf8',
);
const agentEditorSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/AgentEditor.tsx', import.meta.url),
  'utf8',
);
const selectionPaginationSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/SelectionPagination.tsx', import.meta.url),
  'utf8',
);
const pageCardSource = readFileSync(new URL('../src/components/ui/PageCard/PageCard.tsx', import.meta.url), 'utf8');
const groupUploadSource = readFileSync(
  new URL('../src/components/AgentManagementPanel/AgentGroupUploadDialog.tsx', import.meta.url),
  'utf8',
);
const inputAreaSource = readFileSync(new URL('../src/components/ChatPanel/InputArea.tsx', import.meta.url), 'utf8');
const appSource = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');

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

for (const detailStatus of ['loading', 'error']) {
  test(`expert ${detailStatus} keeps the back bar outside the centered content`, async () => {
    const { DefinitionDetailPage } = await import('../node_modules/.cache/agent-management-layout/DefinitionDetailPage.mjs');
    const { JSDOM } = await import('jsdom');
    const markup = renderToStaticMarkup(React.createElement(DefinitionDetailPage, {
      detail: null, detailStatus, detailError: 'Unavailable', onBack() {}, onRetry() {},
    }));
    const document = new JSDOM(markup).window.document;
    const shell = document.querySelector('[data-testid="agent-detail"]');
    assert.ok(shell, 'Loading and errors must reuse the normal detail shell');
    assert.equal(shell.classList.contains('agent-management-detail--state'), false);
    const back = shell.querySelector('[data-testid="agent-management-detail-back"]');
    assert.equal(back.parentElement, shell);
    const content = shell.querySelector('[data-testid="agent-management-detail-state"]');
    assert.ok(content);
    assert.equal(content.contains(back), false);
    assert.equal(content.getAttribute('role'), detailStatus === 'loading' ? 'status' : 'alert');
    assert.equal(content.querySelector('[data-testid="agent-management-detail-retry"]') !== null, detailStatus === 'error');
  });
}

for (const [source, installed, expected] of [['local', true, 'delete'], ['local', false, 'delete'], ['hub', true, 'uninstall'], ['builtin', true, 'uninstall']]) {
  test(`expert ${source} installed=${installed} uses ${expected}`, async () => {
    const { DefinitionDetailPage } = await import('../node_modules/.cache/agent-management-layout/DefinitionDetailPage.mjs');
    const { JSDOM } = await import('jsdom');
    const detail = { id: 'test', runtimePackageName: 'test', displayName: 'Test', description: '', source, installed, connectionState: 'connected', tags: [], avatarUrl: null, skills: [], tools: [], rails: [], mcps: [], suggestedPrompts: [], pendingConnectors: [], details: '', prompt: '' };
    const markup = renderToStaticMarkup(React.createElement(DefinitionDetailPage, { detail, detailStatus: 'ready', detailTab: 'content', fileEntries: [], onBack() {}, onUninstall() {} }));
    const button = new JSDOM(markup).window.document.querySelector('.agent-management-detail-action--uninstall');
    assert.equal(button.textContent, expected === 'delete' ? '删除' : '卸载');
  });
}

test('unsupported expert files remain selectable and show the unsupported preview state', async () => {
  const { DefinitionDetailPage } =
    await import('../node_modules/.cache/agent-management-layout/DefinitionDetailPage.mjs');
  const { JSDOM } = await import('jsdom');
  const detail = {
    id: 'test',
    runtimePackageName: 'test',
    displayName: 'Test',
    description: '',
    source: 'local',
    installed: true,
    connectionState: 'connected',
    tags: [],
    avatarUrl: null,
    skills: [],
    tools: [],
    rails: [],
    mcps: [],
    suggestedPrompts: [],
    pendingConnectors: [],
    details: '',
    prompt: '',
  };
  const markup = renderToStaticMarkup(
    React.createElement(DefinitionDetailPage, {
      detail,
      detailStatus: 'success',
      detailTab: 'files',
      files: [{ relativePath: 'runtime.bin', kind: 'file', previewable: false }],
      filesStatus: 'success',
      selectedFilePath: 'runtime.bin',
      fileContent: null,
      fileStatus: 'success',
      onBack() {},
      onSelectFile() {},
    }),
  );
  const document = new JSDOM(markup).window.document;
  const fileButton = document.querySelector('[data-testid="agent-management-file-tree-item"]');
  assert.equal(fileButton.getAttribute('data-variant'), 'runtime.bin');
  assert.equal(fileButton.disabled, false);
  assert.equal(fileButton.textContent, 'runtime.bin');
  assert.equal(fileButton.querySelector('[data-testid="agent-management-file-tree-item-unsupported"]'), null);
  assert.ok(
    document.querySelector('[data-testid="agent-management-file-preview-content-state"][data-variant="unsupported"]'),
  );
});

test('expert PDF files render through the shared browser preview', async () => {
  const { DefinitionDetailPage } =
    await import('../node_modules/.cache/agent-management-layout/DefinitionDetailPage.mjs');
  const { JSDOM } = await import('jsdom');
  const detail = {
    id: 'test', runtimePackageName: 'test', displayName: 'Test', description: '', source: 'local', installed: true,
    connectionState: 'connected', tags: [], avatarUrl: null, skills: [], tools: [], rails: [], mcps: [],
    suggestedPrompts: [], pendingConnectors: [], details: '', prompt: '',
  };
  const markup = renderToStaticMarkup(React.createElement(DefinitionDetailPage, {
    detail, detailStatus: 'success', detailTab: 'files',
    files: [{ relativePath: 'guide.pdf', kind: 'file', previewable: true }], filesStatus: 'success',
    selectedFilePath: 'guide.pdf',
    fileContent: { relativePath: 'guide.pdf', content: null, downloadUrl: '/file-api/download?token=pdf' },
    fileStatus: 'success', onBack() {}, onSelectFile() {},
  }));
  const document = new JSDOM(markup).window.document;
  const pdf = document.querySelector('[data-testid="agent-management-file-preview-content-pdf"]');
  assert.equal(pdf.getAttribute('src'), '/file-api/download?token=pdf&inline=1');
});

for (const name of ['MarketCard', 'MyMarketCard']) {
  for (const [state, quickAction, label] of [['connected', 'install', '使用'], ['idle', 'install', '安装'], ['idle', 'connect', '连接']]) {
    test(`${name} ${state}/${quickAction} exposes one text action`, async () => {
      const module = await import(`../node_modules/.cache/agent-management-layout/${name}.mjs`);
      const { JSDOM } = await import('jsdom');
      const html = renderToStaticMarkup(React.createElement(module[name], { title: 'Test', tags: ['变更审查', '行为覆盖', '合入就绪'], description: '', state, quickAction, onUse() {}, onQuickAdd() {}, onQuickInstall() {} }));
      const document = new JSDOM(html).window.document;
      assert.ok(document.querySelector('.entity-header__tags').textContent.includes('变更审查'));
      assert.ok(document.querySelector('.entity-header__tags').textContent.includes('行为覆盖'));
      assert.ok(document.querySelector('.entity-header__tags').textContent.includes('合入就绪'));
      const buttons = document.querySelectorAll('button');
      assert.equal(buttons.length, 1);
      assert.equal(buttons[0].textContent, label);
      assert.ok(buttons[0].classList.contains('connector-market-card-install'));
    });
  }
}
test('Expert and Expert Team management keep the shared page shell and field limits', () => {
  assert.match(panelSource, /<div className="page-shell flex-none"[^>]*>\s*<PageHeader/);
  assert.match(groupCatalogSource, /className="page-shell agent-management-toolbar"/);
  assert.match(groupCatalogSource, /className="page-scroll min-h-0 flex-1 overflow-y-auto"/);
  assert.match(groupCardSource, /<PageCard[\s\S]*className="agent-management-page-card agent-group-card"/);
  assert.match(groupCardSource, /headerTestId="agent-group-card-open"/);
  assert.match(groupCardSource, /<PageCard[\s\S]*testId=\{`agent-group-card-\$\{item\.id\}`\}/);
  assert.match(groupCardSource, /interactive\s*\n?\s*ariaLabel/);
  assert.match(groupCardSource, /className="agent-management-card__actions"/);
  assert.doesNotMatch(groupCardSource, /<article/);
  assert.match(groupEditorSource, /id="agent-management-group-name"[\s\S]*maxLength=\{AGENT_NAME_MAX_LENGTH\}/);
  assert.match(
    groupEditorSource,
    /id="agent-management-group-description"[\s\S]*maxLength=\{AGENT_DESCRIPTION_MAX_LENGTH\}/,
  );
  assert.doesNotMatch(groupEditorSource, /detail-back mb-\[35px\]/);
});

test('empty manual definitions do not synthesize an Other domain tag', () => {
  assert.doesNotMatch(catalogPageSource, /scope === 'mine'[\s\S]*categoryOther/);
  assert.doesNotMatch(groupCardSource, /fallbackTag|categoryOther/);
  assert.doesNotMatch(memberPickerSource, /categoryOther/);
  assert.match(catalogPageSource, /const labelTags: string\[\] \| undefined = item\.tags\.length > 0/);
  assert.match(groupCardSource, /const label = item\.tags\.length > 0[\s\S]*: undefined;/);
  assert.match(memberPickerSource, /const categoryTags = [\s\S]*agent\.tags\.length > 0[\s\S]*: undefined;/);
  assert.match(memberPickerSource, /label=\{categoryTags\}/);
  assert.match(groupDetailSource, /const detailTags = detail\.tags;/);
  assert.match(groupDetailSource, /\.\.\.\(categoryLabel \? \[categoryLabel\] : \[\]\)/);
  assert.doesNotMatch(agentDetailSource, /categoryOther/);
});

test('group card actions keep uninstall in the detail page and install created groups before listing', () => {
  assert.doesNotMatch(groupCardSource, /onUninstall|data-variant=\{item\.installed \? 'uninstall'/);
  assert.doesNotMatch(groupCatalogSource, /onUninstall/);
  assert.match(groupDetailSource, /data-variant="uninstall"/);
  assert.match(panelSource, /groupClient\.createGroup\([\s\S]*groupClient\.installGroup\(result\.id\)[\s\S]*loadGroups\('mine'\)/);
  assert.match(panelSource, /groupClient\.importGroup\(path\)[\s\S]*groupClient\.installGroup\(result\.id\)/);
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
  assert.match(inputAreaSource, /const agentSelectionDisabled = isTeamMode;/);
  assert.match(inputAreaSource, /const agentGroupSelectionDisabled = isAgentMode \|\| agentGroupPickerLocked;/);
  assert.match(inputAreaSource, /aria-disabled=\{agentSelectionDisabled\}[\s\S]*disabled=\{agentSelectionDisabled\}/);
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
  assert.match(inputAreaSource, /!isTeamMode \? \([\s\S]*chat-panel-agent-picker-agent-tab/);
  assert.match(inputAreaSource, /!isAgentMode \? \([\s\S]*chat-panel-agent-picker-agent-group-tab/);
  assert.match(inputAreaSource, /t\(isTeamMode \? 'chat\.agentGroup' : 'chat\.agent'\)/);
});

test('single-mode picker removes its redundant title and More routes to the matching My Expert type', () => {
  assert.match(
    inputAreaSource,
    /tabs=\{\s*!isAgentMode && !isTeamMode \?[\s\S]*chat-panel-agent-picker-agent-tab[\s\S]*chat-panel-agent-picker-agent-group-tab[\s\S]*: null/,
  );
  assert.match(inputAreaSource, /onNavigateToAgents\?\.\(isTeamMode \? 'group' : 'agent'\)/);
  assert.match(panelSource, /navigationRequest\?: \{\s*target: 'agent' \| 'group';\s*requestId: number;/);
  assert.match(panelSource, /setView\('mine'\)[\s\S]*setMineKind\(request\.target\)/);
  assert.match(appSource, /onNavigateToAgents=\{handleNavigateToAgentManagement\}/);
  assert.match(appSource, /navigationRequest=\{agentManagementNavigationRequest\}/);
});

test('leader and member picker cards include the shared Expert description', () => {
  assert.match(memberPickerSource, /import \{ PageCard, Tabs \} from '\.\.\/ui';/);
  assert.match(memberPickerSource, /<PageCard[\s\S]*className=\{`agent-management-selection-card agent-management-selection-card--expert/);
  assert.match(memberPickerSource, /description=\{description\}/);
  assert.match(
    memberPickerSource,
    /const description = agent\.description \|\| t\('agentManagement\.unknownDescription'\);/,
  );
  assert.match(
    pageCardSource,
    /selected\?: boolean;[\s\S]*disabled\?: boolean;/,
  );
  assert.match(
    agentManagementCss,
    /\.agent-management-selection-card\.page-card \.entity-header\s*\{\s*width: 100%;\s*min-width: 0;/,
  );
  assert.match(
    agentManagementCss,
    /\.agent-management-selection-card\.page-card \.page-card__body\s*\{[\s\S]*overflow-wrap: anywhere;[\s\S]*text-align: left;/,
  );
});

test('management pickers expose source tabs and preserve install/connect actions', () => {
  assert.match(
    panelSource,
    /scheduleCatalogRefresh\('agent-catalog', marketplaceCatalog\.cache, \(\) => \{ void loadCatalog\(options\); \}/,
  );
  assert.match(panelSource, /const skillsRevisionRef = useRef\(0\)/);
  assert.match(panelSource, /if \(revision !== skillsRevisionRef\.current\) return;/);
  assert.match(panelSource, /onTeamMarketplaceLoaded:/);
  assert.match(panelSource, /scheduleCatalogRefresh\(\s*'agent-team-skill-marketplace'/);
  assert.match(panelSource, /void loadCatalog\(view === 'group-create' \? \{ includeTeamCompatibility: true \} : \{\}\);/);
  assert.match(panelSource, /catalog\.compatibility\.loading/);
  assert.match(panelSource, /catalog\.compatibility\.error/);
  assert.match(panelSource, /agentsStatus=\{state\.catalogCompatibilityStatus\}/);
  assert.match(groupEditorSource, /agentsError=\{agentsError\}/);
  assert.match(groupEditorSource, /onReloadAgents=\{onReloadAgents\}/);
  assert.match(memberPickerSource, /agent-group-member-picker-tabs/);
  assert.match(memberPickerSource, /agent-group-member-picker-install/);
  assert.match(memberPickerSource, /agent-group-member-picker-error/);
  assert.match(memberPickerSource, /onReloadAgents/);
  assert.match(memberPickerSource, /agent-group-member-picker-pagination/);
  assert.match(memberPickerSource, /agent-group-member-picker-tab-market/);
  assert.match(memberPickerSource, /agent-group-member-picker-tab-local/);
  assert.match(memberPickerSource, /sortAgentGroupOptions\(sourceAgents, agentsStatus\)/);
  assert.match(memberPickerSource, /isAgentGroupAgentCompatibilityLoading\(agent, agentsStatus\)/);
  assert.match(memberPickerSource, /className=.*is-loading/);
  assert.match(
    memberPickerSource,
    /sourceTab === 'market' \? agent\.source !== 'local' : agent\.source === 'local' \|\| agent\.installed === true/,
  );
  assert.match(memberPickerSource, /useState<\s*'local' \| 'market'\s*>\('market'\)/);
  assert.match(groupEditorSource, /isTeamSkillOption\(skill, skillSourceTab\)/);
  assert.match(groupEditorSource, /isSkillVisibleInSourceTab\(skill, skillSourceTab\)/);
  assert.match(groupEditorSource, /agent-group-editor-skill-picker-install/);
  assert.match(groupEditorSource, /skillSourceTab/);
  assert.match(groupEditorSource, /agent-group-editor-skill-picker-tabs/);
  assert.match(groupEditorSource, /agent-group-editor-skill-picker-pagination/);
  assert.match(groupEditorSource, /agent-group-editor-skill-picker-tab-market/);
  assert.match(groupEditorSource, /agent-group-editor-skill-picker-tab-local/);
  assert.match(groupEditorSource, /useState<\s*'local' \| 'market'\s*>\('market'\)/);
  assert.match(agentEditorSource, /agent-editor-skill-picker-tabs/);
  assert.match(agentEditorSource, /agent-editor-skill-picker-pagination/);
  assert.match(agentEditorSource, /agent-editor-mcp-picker-tabs/);
  assert.match(agentEditorSource, /agent-editor-mcp-picker-pagination/);
  assert.match(agentEditorSource, /agent-editor-mcp-picker-connect/);
  assert.match(agentEditorSource, /sortMcpOptions\(\s*mcpOptions\.filter\([\s\S]*?mcpSourceTab/);
  assert.match(agentEditorSource, /const selectable = isMcpSelectable\(mcp\)/);
  assert.match(agentEditorSource, /interactive=\{selectable\}/);
  assert.match(agentEditorSource, /agent-editor-skill-picker-tab-market/);
  assert.match(agentEditorSource, /agent-editor-skill-picker-tab-local/);
  assert.match(agentEditorSource, /isTeamSkillOption\(skill, skillSourceTab\)/);
  assert.match(agentEditorSource, /agent-editor-mcp-picker-tab-market/);
  assert.match(agentEditorSource, /agent-editor-mcp-picker-tab-installed/);
  assert.match(agentEditorSource, /useState<\s*'local' \| 'market'\s*>\('market'\)/);
  assert.doesNotMatch(agentEditorSource, /MCP_TYPE_OPTIONS|mcpTypeFilter|mcpTypeAll/);
  assert.match(panelSource, /const \[busySkillId, setBusySkillId\] = useState<string \| null>\(null\)/);
  assert.match(panelSource, /const \[busyMcpId, setBusyMcpId\] = useState<string \| null>\(null\)/);
  assert.match(panelSource, /setBusySkillId\(skill\.id\)/);
  assert.match(panelSource, /setBusyMcpId\(mcp\.id\)/);
  assert.match(panelSource, /installingSkillId=\{busySkillId\}/);
  assert.match(panelSource, /installingMcpId=\{busyMcpId\}/);
  assert.match(selectionPaginationSource, /SELECTION_PAGE_SIZE = 10/);
  assert.match(
    agentManagementCss,
    /agent-management-selection-card\.page-card\.is-disabled[\s\S]*opacity: 1;/,
  );
  assert.match(
    agentManagementCss,
    /agent-management-selection-card\.page-card\.is-disabled \.entity-header__tag[\s\S]*border: 1px solid var\(--color-border-default\);/,
  );
  assert.match(
    agentManagementCss,
    /agent-management-selection-card\.page-card\.is-loading[\s\S]*cursor: wait[\s\S]*opacity: 1;/,
  );
  assert.match(agentManagementCss, /agent-management-selection-card__loading-icon[\s\S]*animation:/);
  assert.match(
    agentManagementCss,
    /agent-management-selection-card__install[\s\S]*color: var\(--color-action-primary\);/,
  );
  assert.match(
    agentManagementCss,
    /\.agent-management-selection-card\.page-card\.is-selected\s*\{[\s\S]*box-shadow: inset/,
  );
  assert.doesNotMatch(
    agentManagementCss,
    /\.agent-management-selection-card\.page-card:hover,[\s\S]*\.agent-management-selection-card\.page-card:focus-within/,
  );
});

test('Expert Team chat creation uses the standard new-chat welcome in Agent mode', () => {
  const groupCreateNavigation = appSource.match(
    /onCreateGroupViaChat=\{\(\) => requestSessionNavigation\('new', \{[\s\S]*?\}\)\}/,
  )?.[0];
  assert.ok(groupCreateNavigation);
  assert.match(groupCreateNavigation, /initialSelectedSkills: \['agent-group-creator'\]/);
  assert.match(groupCreateNavigation, /forceMode: 'agent'/);
  assert.doesNotMatch(groupCreateNavigation, /welcomeVariant/);
});

test('Expert and Expert Team content details use the same full-width markdown layout', () => {
  assert.match(agentDetailSource, /<MarkdownPane[\s\S]*testId="agent-management-detail-content"/);
  assert.match(groupDetailSource, /<MarkdownPane[\s\S]*testId="agent-group-detail-content"/);
});

test('Expert Team cards and details reuse the Expert visual primitives', () => {
  assert.match(catalogPageSource, /<PageCard[\s\S]*className="agent-management-page-card agent-definition-card(?:\s|")/);
  assert.match(groupCardSource, /import \{ PageCard \} from '\.\.\/ui';/);
  assert.match(groupCardSource, /className="agent-management-page-card agent-group-card"/);
  assert.match(groupCardSource, /<PageCard[\s\S]*testId=\{`agent-group-card-\$\{item\.id\}`\}/);
  assert.match(groupCardSource, /<PageCard[\s\S]*interactive/);
  assert.match(
    groupCardSource,
    /className="agent-management-card__actions"[\s\S]*onClick=\{\(event\) => event\.stopPropagation\(\)\}/,
  );
  assert.match(agentManagementCss, /\.agent-management-page-card \.entity-header__actions\s*\{\s*display: contents;/);
  assert.match(agentManagementCss, /\.agent-management-page-card \.entity-header__identity\s*\{\s*flex: 1 1 auto;/);
  assert.match(
    agentManagementCss,
    /\.agent-management-page-card \.agent-management-card__actions\s*\{[\s\S]*position: absolute;/,
  );
  assert.match(
    agentManagementCss,
    /@media \(max-width: 800px\)[\s\S]*\.agent-management-page-card \.agent-management-card__actions\s*\{[\s\S]*top: auto;/,
  );
  assert.match(agentManagementCss, /\.agent-management-page-card:focus-within \.agent-management-card__actions/);
  assert.match(entityHeaderCss, /\.entity-header__tags\s*\{[\s\S]*width: 100%;/);
  assert.match(
    agentManagementCss,
    /@media \(min-width: 801px\)[\s\S]*\.agent-management-page-card:focus-within \.entity-header__identity/,
  );
  assert.match(groupDetailSource, /import \{[^}]*EntityHeader[^}]*\} from '\.\.\/ui';/);
  assert.match(groupDetailSource, /import \{[^}]*DetailSection[^}]*\} from '\.\.\/ui';/);
  assert.match(groupDetailSource, /import \{[^}]*Tabs[^}]*\} from '\.\.\/ui';/);
  assert.match(groupDetailSource, /<EntityHeader[\s\S]*testId="agent-management-detail-header"/);
  assert.match(groupDetailSource, /<DetailSection[\s\S]*testId="agent-management-detail-ability"/);
  assert.match(groupDetailSource, /<PageToolbar[\s\S]*<Tabs[\s\S]*wrapperTestId="agent-group-detail-tabs"/);
  assert.match(groupDetailSource, /className="detail-back"/);
  assert.match(groupDetailSource, /className="detail-body flex-1 min-h-0 overflow-y-auto"/);
  assert.doesNotMatch(groupDetailSource, /detail-back mb-\[35px\]/);
  assert.doesNotMatch(groupDetailSource, /overflow-y-auto pb-\[72px\]/);
  assert.doesNotMatch(groupDetailSource, /<header className="agent-management-detail__header">/);
  assert.match(groupDetailSource, /<PublicationDetailStatus kind="agent_group"/);
  assert.match(groupDetailSource, /openAssetPublish\(\{ kind: 'agent_group', local_id: detail\.id/);
});

test('installed Expert Team cards offer use while uninstall stays in detail', async () => {
  const { GroupCard } = await import('../node_modules/.cache/agent-management-layout/GroupCard.mjs');
  const { JSDOM } = await import('jsdom');
  const item = {
    id: 'group-1', displayName: '测试专家团', description: '测试', category: '',
    tags: [], avatarUrl: null, installed: true,
    capabilities: { canUse: true, canInstall: false, canUninstall: true },
  };
  const markup = renderToStaticMarkup(React.createElement(GroupCard, {
    item, busy: false, onOpen: () => {}, onUse: () => {}, onInstall: () => {},
  }));
  const card = new JSDOM(markup).window.document;
  assert.equal(card.querySelectorAll('[data-testid="agent-group-card-action"]').length, 1);
  assert.equal(card.querySelector('[data-testid="agent-group-card-action"]').getAttribute('data-variant'), 'use');
  assert.equal(card.querySelector('[data-variant="uninstall"]'), null);
  assert.match(groupDetailSource, /data-variant="uninstall"/);
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

for (const status of ['success', 'loading', 'error']) {
  test(`expert catalog keeps page two cards during ${status}`, async () => {
    const { CatalogPage } = await import('../node_modules/.cache/agent-management-layout/CatalogPage.mjs');
    const { JSDOM } = await import('jsdom');
    const items = Array.from({ length: 20 }, (_, i) => ({ id: `expert-${i}`, runtimePackageName: `expert-${i}`, displayName: `Expert ${i}`, description: '', source: 'local', installed: true, connectionState: 'connected', tags: [], avatarUrl: null }));
    const document = new JSDOM(renderToStaticMarkup(React.createElement(CatalogPage, {
      scope: 'mine', items, totalItems: items.length, page: 2, query: '', category: '', status, error: 'Refresh failed', onPageChange() {},
    }))).window.document;
    const cards = document.querySelectorAll('[data-testid="agent-card"]');
    assert.equal(cards.length, 5);
    assert.equal(cards[0].getAttribute('data-variant'), 'expert-15');
    assert.equal(document.querySelector('[data-testid="agent-catalog-page-previous"]').disabled, false);
    assert.equal(document.querySelector('[data-testid="agent-catalog-page-next"]').disabled, true);
    for (const card of cards) {
      assert.equal(card.querySelectorAll('button').length, 1);
      assert.ok(card.querySelector('.agent-management-card-action--use'));
    }
  });
}

test('expert selection matches Hub identity or runtime package without clearing a missing catalog entry', () => {
  assert.match(inputAreaSource, /item\.id === selectedId \|\| item\.runtimePackageName === selectedId/);
  assert.match(inputAreaSource, /selectedItem &&\s*\(selectedItem\.enabled === false/);
});
