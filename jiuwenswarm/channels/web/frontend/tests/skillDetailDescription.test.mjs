import assert from 'node:assert/strict';
import { after, test } from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';
import esbuild from 'esbuild';
import React, { act } from 'react';
import { JSDOM } from 'jsdom';
import i18next from 'i18next';
import { initReactI18next } from 'react-i18next';

const outfile = fileURLToPath(new URL('../node_modules/.cache/skill-detail/SkillDetailView.mjs', import.meta.url));
await esbuild.build({
  entryPoints: [fileURLToPath(new URL('../src/components/SkillPanel/SkillDetailView.tsx', import.meta.url))],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  outfile,
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.png': 'dataurl', '.svg': 'dataurl' },
  plugins: [{
    name: 'svg-react-stub',
    setup(build) {
      build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({ path: args.path, namespace: 'svg-react-stub' }));
      build.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
        contents: 'export default function Icon() { return null; }',
        loader: 'js',
      }));
    },
  }],
});

const dom = new JSDOM('<!DOCTYPE html><div id="root"></div>', { url: 'http://localhost/' });
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
const { SkillDetailView } = await import(`${pathToFileURL(outfile).href}?build=${Date.now()}`);

const noop = () => {};
const skill = {
  name: 'issue-writer',
  description: '第一段说明\n第二段说明',
  source: 'local',
  version: '1.0.0',
  author: 'tester',
  tags: [],
  allowed_tools: [],
  content: '',
  file_path: 'SKILL.md',
};

test('installed skill basic info keeps description line breaks', async () => {
  const root = createRoot(document.getElementById('root'));
  await act(async () => {
    root.render(React.createElement(SkillDetailView, {
      mode: 'installed',
      detailState: 'success',
      installedSkillMap: new Map(),
      selectedSkill: skill,
      detailContent: '',
      detailTab: 'content',
      setDetailTab: noop,
      detailMenuOpen: false,
      setDetailMenuOpen: noop,
      actionTarget: null,
      skillVersions: [],
      skillVersionsDefault: null,
      versionsLoadState: 'success',
      onFetchSkillDetail: noop,
      filesLoadState: 'idle',
      filePreviewPath: null,
      filePreviewStatus: 'idle',
      previewTreeNodes: [],
      filePreview: null,
      onSelectSkillFile: noop,
      onFetchSkillFiles: noop,
      evolutionMessage: null,
      evolutionMessageType: null,
      evolutionFormatError: null,
      evolutionListState: 'idle',
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
    }));
  });
  const basicInfo = document.querySelector('[data-testid="skill-panel-my-detail-basic-info"]');
  const description = basicInfo?.querySelector('.whitespace-pre-line');
  assert.equal(description?.textContent, '第一段说明\n第二段说明');
  root.unmount();
});
