import assert from 'node:assert/strict';
import test from 'node:test';
import path from 'node:path';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';

// ?react 的 SVGR 图标在 node 测试里以空组件桩掉（与 buildJiuwenmateProfile.mjs 同法）
const svgReact = {
  name: 'svg-react',
  setup(esbuild) {
    esbuild.onResolve({ filter: /\.svg\?react$/ }, (args) => ({
      path: path.resolve(args.resolveDir, args.path.replace(/\?react$/, '')),
      namespace: 'svg-react',
    }));
    esbuild.onLoad({ filter: /\.svg$/, namespace: 'svg-react' }, () => ({
      contents: 'export default function Svg() { return null; }',
      loader: 'js',
    }));
  },
};

await build({
  absWorkingDir: process.cwd(),
  entryPoints: ['src/components/SkillPanel/SkillDetailView.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  jsx: 'automatic',
  outfile: 'node_modules/.cache/skill-detail-description/SkillDetailView.mjs',
  define: { 'import.meta.env.DEV': 'false' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [svgReact],
});
const { SkillDetailView } = await import('../node_modules/.cache/skill-detail-description/SkillDetailView.mjs');
const i18n = createInstance();
await i18n.init({
  lng: 'zh',
  resources: { zh: { translation: {} } },
  interpolation: { escapeValue: false },
});

const noop = () => {};

function buildInstalledProps(description) {
  return {
    mode: 'installed',
    detailState: 'success',
    installedSkillMap: new Map(),
    selectedSkill: {
      name: 'demo-skill',
      display_name: 'Demo Skill',
      description,
      source: 'local',
      version: '1.0.0',
      author: 'tester',
      tags: [],
      allowed_tools: [],
      skill_type: 'skill',
      enabled: true,
      has_evolutions: false,
      content: '# demo',
      file_path: 'skills/demo-skill/SKILL.md',
    },
    detailContent: '# demo',
    detailTab: 'content',
    setDetailTab: noop,
    detailMenuOpen: false,
    setDetailMenuOpen: noop,
    actionTarget: null,
    skillVersions: [],
    skillVersionsDefault: null,
    versionsLoadState: 'success',
    onFetchSkillDetail: noop,
    filesLoadState: 'success',
    filePreviewPath: null,
    filePreviewStatus: 'success',
    previewTreeNodes: [],
    filePreview: null,
    onSelectSkillFile: noop,
    onFetchSkillFiles: noop,
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
  };
}

test('skill detail basic info preserves description line breaks (#6672)', () => {
  const description = '第一行说明：负责数据整理与清洗。\n第二行说明：输出汇总报告。\n\n末尾备注。';
  const markup = renderToStaticMarkup(createElement(I18nextProvider, { i18n },
    createElement(SkillDetailView, buildInstalledProps(description))));

  const wrapped = /<p class="whitespace-pre-line">([\s\S]*?)<\/p>/.exec(markup);
  assert.ok(wrapped, 'description should render inside a whitespace-pre-line paragraph');
  assert.ok(wrapped[1].includes('第一行说明：负责数据整理与清洗。'));
  assert.ok(wrapped[1].includes('\n第二行说明：输出汇总报告。'), 'newline between lines must be kept');
  assert.ok(wrapped[1].includes('\n\n末尾备注。'), 'blank line must be kept');
  assert.ok(!/whitespace-(nowrap|normal)/.test(wrapped[1]));
});

test('single-line descriptions keep the same wrapping container', () => {
  const markup = renderToStaticMarkup(createElement(I18nextProvider, { i18n },
    createElement(SkillDetailView, buildInstalledProps('一句话描述'))));

  const wrapped = /<p class="whitespace-pre-line">([\s\S]*?)<\/p>/.exec(markup);
  assert.ok(wrapped, 'description container should exist');
  assert.equal(wrapped[1], '一句话描述');
});
