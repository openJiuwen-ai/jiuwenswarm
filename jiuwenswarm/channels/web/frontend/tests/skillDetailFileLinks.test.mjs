import assert from 'node:assert/strict';
import { after, afterEach, test } from 'node:test';
import React, { act, useState } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const dom = new JSDOM('<!doctype html><html><body><div id="root"></div></body></html>', {
  url: 'http://localhost/',
});
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
  lng: 'en',
  showSupportNotice: false,
  resources: { en: { translation: {} } },
});

const { useSkillFilesTab } = await import('../node_modules/.cache/skill-marketplace/useSkillFilesTab.mjs');
const { SkillDetailView } = await import('../node_modules/.cache/skill-marketplace/SkillDetailView.mjs');
const { __setWebRequestHandler } = await import(
  '../node_modules/.cache/skill-marketplace/skillDetailWebClientStub.mjs'
);

const withSession = (params = {}) => ({ ...params, session_id: 'test-session' });
const noop = () => {};
const skillFiles = [
  { path: 'SKILL.md', type: 'file', size: 20, mime_type: 'text/markdown' },
  { path: 'references/api.md', type: 'file', size: 20, mime_type: 'text/markdown' },
  { path: 'references/target.md', type: 'file', size: 20, mime_type: 'text/markdown' },
];
const selectedSkill = {
  name: 'demo',
  display_name: 'Demo Skill',
  description: 'Demo skill',
  content: '[Target guide](references/target.md)',
  file_path: 'C:\\skills\\demo\\SKILL.md',
  version: '1.0.0',
  source: 'local',
  source_type: 'user',
  origin: 'local',
  is_builtin: false,
  is_builtin_source: false,
  has_evolutions: false,
  skill_type: 'skill',
};

function installWebRequestMock() {
  const calls = [];
  __setWebRequestHandler(async (method, params = {}) => {
    calls.push({ method, params });
    if (method === 'skills.files.list') {
      return { name: String(params.name), files: skillFiles };
    }
    if (method === 'skills.files.get') {
      const path = String(params.path);
      return {
        name: String(params.name),
        path,
        content: `# ${path}`,
        mime_type: 'text/markdown',
        size: 20,
        download_url: null,
      };
    }
    throw new Error(`Unexpected webRequest method: ${method}`);
  });
  return calls;
}

afterEach(() => {
  __setWebRequestHandler(async () => {
    throw new Error('webRequest handler has not been configured');
  });
});

async function waitFor(predicate, message) {
  const deadline = Date.now() + 1000;
  while (!predicate()) {
    if (Date.now() > deadline) throw new Error(message);
    await act(async () => new Promise((resolve) => dom.window.setTimeout(resolve, 5)));
  }
}

function HookHarness({ capture }) {
  capture(useSkillFilesTab({ withSession }));
  return null;
}

test('initial file list keeps an explicitly requested file over the default preview', async () => {
  const calls = installWebRequestMock();
  let api = null;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        React.createElement(HookHarness, {
          capture: (value) => {
            api = value;
          },
        }),
      ),
    );
    assert.ok(api);
    await act(async () => api.fetchSkillFiles('demo', 'references/target.md'));

    assert.equal(api.filesLoadState, 'success');
    assert.equal(api.filePreviewPath, 'references/target.md');
    assert.deepEqual(
      calls.map(({ method, params }) => ({ method, path: params.path || null })),
      [
        { method: 'skills.files.list', path: null },
        { method: 'skills.files.get', path: 'references/target.md' },
      ],
    );
  } finally {
    await act(async () => root.unmount());
  }
});

function SkillDetailHarness({ capture }) {
  const [detailTab, setDetailTab] = useState('content');
  const [detailMenuOpen, setDetailMenuOpen] = useState(false);
  const files = useSkillFilesTab({ withSession });
  capture({
    detailTab,
    filePreviewPath: files.filePreviewPath,
    filesLoadState: files.filesLoadState,
  });

  return React.createElement(SkillDetailView, {
    mode: 'installed',
    selectedSkill,
    detailState: 'success',
    installedSkillMap: new Map(),
    detailContent: selectedSkill.content,
    detailTab,
    setDetailTab,
    detailMenuOpen,
    setDetailMenuOpen,
    actionTarget: null,
    skillVersions: [],
    skillVersionsDefault: null,
    versionsLoadState: 'success',
    onFetchSkillDetail: noop,
    filesLoadState: files.filesLoadState,
    filePreviewPath: files.filePreviewPath,
    filePreviewStatus: files.filePreviewStatus,
    previewTreeNodes: files.previewTreeNodes,
    filePreview: files.filePreview,
    onSelectSkillFile: (filePath) => {
      void files.fetchFilePreview('demo', filePath);
    },
    onFetchSkillFiles: files.fetchSkillFiles,
    evolutionMessage: null,
    evolutionMessageType: null,
    evolutionFormatError: null,
    evolutionListState: 'success',
    sortedEvolutionEntries: [],
    onEvolutionContentChange: noop,
    onEvolutionDeleteEntry: noop,
    rebuildLoading: false,
    onRebuild: noop,
    setSynthesizeTooltip: noop,
    onBack: noop,
    onEditSkill: noop,
    onUninstall: noop,
    onToggleSkillDisabled: noop,
    onGoToChat: noop,
    onOpenPackMember: noop,
  });
}

test('clicking a relative content link opens the resolved file preview', async () => {
  const calls = installWebRequestMock();
  let state = null;
  const root = createRoot(document.getElementById('root'));
  try {
    await act(async () =>
      root.render(
        React.createElement(SkillDetailHarness, {
          capture: (value) => {
            state = value;
          },
        }),
      ),
    );

    const content = document.querySelector('[data-testid="skill-panel-my-detail-content"]');
    assert.ok(content);
    const link = content.querySelector('a[href="references/target.md"]');
    assert.ok(link);
    const event = new dom.window.MouseEvent('click', { bubbles: true, cancelable: true });
    await act(async () => link.dispatchEvent(event));

    await waitFor(
      () =>
        state?.detailTab === 'files' &&
        state?.filesLoadState === 'success' &&
        state?.filePreviewPath === 'references/target.md',
      'content link did not switch to and load the resolved file preview',
    );

    assert.equal(event.defaultPrevented, true);
    assert.equal(link.hasAttribute('target'), false);
    assert.deepEqual(
      calls
        .filter(({ method }) => method === 'skills.files.get')
        .map(({ params }) => params.path),
      ['references/target.md'],
    );
  } finally {
    await act(async () => root.unmount());
  }
});
