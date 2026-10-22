import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import path from 'node:path';
import test from 'node:test';
import { fileURLToPath } from 'node:url';
import { JSDOM } from 'jsdom';
import * as esbuild from 'esbuild';
import i18next from 'i18next';
import React, { act } from 'react';
import { createRoot } from 'react-dom/client';
import { I18nextProvider, initReactI18next } from 'react-i18next';

const frontendRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const svgReactPlugin = {
  name: 'svg-react',
  setup(build) {
    build.onResolve({ filter: /\.svg\?react$/ }, (args) => ({
      path: path.resolve(args.resolveDir, args.path.replace(/\?react$/, '')),
      namespace: 'svg-react',
    }));
    build.onLoad({ filter: /\.svg$/, namespace: 'svg-react' }, () => ({
      contents: 'export default function Svg() { return null; }',
      loader: 'js',
    }));
  },
};

await esbuild.build({
  absWorkingDir: frontendRoot,
  entryPoints: ['src/features/jiuwenmate/pages/ColleagueCreatePage.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/jiuwenmate-create-install-ui/ColleagueCreatePage.js',
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl', '.webp': 'dataurl' },
  plugins: [svgReactPlugin],
  jsx: 'automatic',
  define: { 'import.meta.env.DEV': 'false' },
});

const { ColleagueCreatePage } = await import(
  '../node_modules/.cache/jiuwenmate-create-install-ui/ColleagueCreatePage.js'
);
const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const i18n = i18next.createInstance();
await i18n.use(initReactI18next).init({
  lng: 'zh',
  resources: { zh: { translation: zh } },
  interpolation: { escapeValue: false },
});

test('template skill install errors use a dialog and the install action is visually secondary', async () => {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', { pretendToBeVisual: true });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: dom.window.HTMLElement });
  Object.defineProperty(globalThis, 'Node', { configurable: true, value: dom.window.Node });
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { configurable: true, value: true });

  const css = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');
  const selectors = [
    '.jiuwenmate-create__skill-recommendations',
    '.jiuwenmate-create__skill-action',
    '.jiuwenmate-create__skill-name',
    '.jiuwenmate-create__install-button',
  ];
  const stylesheet = dom.window.document.createElement('style');
  stylesheet.textContent = selectors
    .map((selector) => css.match(new RegExp(`${selector.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')}\\s*\\{[^{}]*\\}`))?.[0])
    .filter(Boolean)
    .join('\n');
  dom.window.document.head.append(stylesheet);

  const container = dom.window.document.createElement('div');
  dom.window.document.body.append(container);
  const root = createRoot(container);
  const draft = {
    name: '系统架构工程师',
    description: '梳理模块依赖并评审方案。',
    tags: ['研发工程'],
    category: 'development',
    capabilities: {
      model: '',
      skills: [],
      connectors: [],
      plugins: [],
      knowledge: [],
      starterPrompts: [],
    },
  };

  await act(async () => {
    root.render(
      React.createElement(
        I18nextProvider,
        { i18n },
        React.createElement(ColleagueCreatePage, {
          step: 'refine',
          source: 'template',
          detailTemplateId: null,
          customDialogOpen: false,
          draft,
          modelCatalog: { models: [], defaultModelId: '' },
          resourceCatalog: { skills: [], connectors: [], plugins: [], knowledge: [] },
          resourceCatalogReady: true,
          missingTemplateCapabilities: { skills: [], connectors: [], plugins: [], knowledge: [] },
          skillRecommendations: {
            status: 'ready',
            local: [],
            plaza: [{
              source: 'teamskillshub',
              name: 'architecture-review',
              display_name: '架构评审',
              identifier: 'asset-42',
              description: '审查系统架构方案',
            }],
            error: null,
          },
          onDismissSkillRecommendationError() {},
          onInstallSkill: async () => {
            throw new Error('没有找到唯一的技能安装来源');
          },
          submitting: false,
          onOpenTemplateDetail() {},
          onCloseTemplateDetail() {},
          onChooseTemplate() {},
          onOpenCustom() {},
          onCloseCustom() {},
          onConfirmCustom() {},
          onOpenPlaza() {},
          onDraftChange() {},
          onCancel() {},
          onComplete() {},
        }),
      ),
    );
  });

  const item = container.querySelector('[data-testid="jiuwenmate-template-plaza-skill-0"]');
  const skillName = item?.querySelector('.jiuwenmate-create__skill-name');
  const installButton = item?.querySelector('[data-testid="jiuwenmate-install-recommended-plaza-skill-0"]');
  assert.ok(skillName, 'the plaza skill recommendation should be visible');
  assert.ok(installButton, 'the plaza skill recommendation should offer installation');
  assert.equal(dom.window.getComputedStyle(skillName).fontSize, '12px');
  assert.equal(dom.window.getComputedStyle(installButton).fontSize, '11px');

  await act(async () => {
    installButton.click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });

  const dialog = container.querySelector('[role="dialog"][data-testid="jiuwenmate-install-skill-error-dialog"]');
  assert.ok(dialog, 'a failed search or install should open a page dialog');
  assert.match(dialog.textContent, /没有找到唯一的技能安装来源/);
  assert.equal(item.querySelector('[role="alert"]'), null, 'the error should not be placed beside the recommendation');
  const confirmButton = dialog.querySelector('[data-testid="jiuwenmate-install-skill-error-confirm"]');
  assert.ok(confirmButton, 'the dialog should have a visible confirmation action');
  await act(async () => confirmButton.click());
  assert.equal(
    container.querySelector('[role="dialog"][data-testid="jiuwenmate-install-skill-error-dialog"]'),
    null,
    'confirming the error should close the dialog',
  );

  await act(async () => root.unmount());
  dom.window.close();
});

test('template recommendations separate locally installed skills from plaza skills and add the chosen result', async () => {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', { pretendToBeVisual: true });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: dom.window.HTMLElement });
  Object.defineProperty(globalThis, 'Node', { configurable: true, value: dom.window.Node });
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { configurable: true, value: true });

  const container = dom.window.document.createElement('div');
  dom.window.document.body.append(container);
  const root = createRoot(container);
  const draft = {
    name: '系统架构工程师',
    description: '梳理模块依赖并评审方案。',
    tags: ['研发工程'],
    category: 'development',
    capabilities: { model: '', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
  };
  const candidate = {
    source: 'teamskillshub',
    name: 'architecture-review',
    display_name: '架构评审',
    identifier: 'asset-42',
    description: '审查系统架构方案',
  };
  const patches = [];
  let installedCandidate = null;

  await act(async () => {
    root.render(
      React.createElement(
        I18nextProvider,
        { i18n },
        React.createElement(ColleagueCreatePage, {
          step: 'refine',
          source: 'template',
          detailTemplateId: null,
          customDialogOpen: false,
          draft,
          modelCatalog: { models: [], defaultModelId: '' },
          resourceCatalog: { skills: [], connectors: [], plugins: [], knowledge: [] },
          resourceCatalogReady: true,
          missingTemplateCapabilities: { skills: [], connectors: [], plugins: [], knowledge: [] },
          skillRecommendations: {
            status: 'ready',
            local: [{ name: 'code-review', label: '代码审查', description: '审查代码变更' }],
            plaza: [candidate],
            error: null,
          },
          onDismissSkillRecommendationError() {},
          onInstallSkill: async (skill) => {
            installedCandidate = skill;
            return 'architecture-review';
          },
          submitting: false,
          onOpenTemplateDetail() {},
          onCloseTemplateDetail() {},
          onChooseTemplate() {},
          onOpenCustom() {},
          onCloseCustom() {},
          onConfirmCustom() {},
          onOpenPlaza() {},
          onDraftChange: (patch) => patches.push(patch),
          onCancel() {},
          onComplete() {},
        }),
      ),
    );
  });

  assert.match(container.textContent, /本地已安装/);
  assert.match(container.textContent, /技能广场/);
  assert.match(container.textContent, /代码审查/);
  assert.match(container.textContent, /架构评审/);

  await act(async () => {
    container.querySelector('[data-testid="jiuwenmate-add-recommended-local-skill-0"]').click();
  });
  assert.deepEqual(patches.at(-1).capabilities.skills, ['code-review']);

  await act(async () => {
    container.querySelector('[data-testid="jiuwenmate-install-recommended-plaza-skill-0"]').click();
    await new Promise((resolve) => setTimeout(resolve, 0));
  });
  assert.deepEqual(installedCandidate, candidate);
  assert.deepEqual(patches.at(-1).capabilities.skills, ['architecture-review']);

  await act(async () => root.unmount());
  dom.window.close();
});

test('template plaza lookup failures appear in a dialog, not beside recommendations', async () => {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', { pretendToBeVisual: true });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: dom.window.HTMLElement });
  Object.defineProperty(globalThis, 'Node', { configurable: true, value: dom.window.Node });
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { configurable: true, value: true });

  const container = dom.window.document.createElement('div');
  dom.window.document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      React.createElement(
        I18nextProvider,
        { i18n },
        React.createElement(ColleagueCreatePage, {
          step: 'refine',
          source: 'template',
          detailTemplateId: null,
          customDialogOpen: false,
          draft: {
            name: '系统架构工程师',
            description: '梳理模块依赖并评审方案。',
            tags: [],
            category: 'development',
            capabilities: { model: '', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
          },
          modelCatalog: { models: [], defaultModelId: '' },
          resourceCatalog: { skills: [], connectors: [], plugins: [], knowledge: [] },
          resourceCatalogReady: true,
          missingTemplateCapabilities: { skills: [], connectors: [], plugins: [], knowledge: [] },
          skillRecommendations: { status: 'error', local: [], plaza: [], error: '技能广场搜索暂不可用' },
          onDismissSkillRecommendationError() {},
          onInstallSkill: async () => 'unused',
          submitting: false,
          onOpenTemplateDetail() {},
          onCloseTemplateDetail() {},
          onChooseTemplate() {},
          onOpenCustom() {},
          onCloseCustom() {},
          onConfirmCustom() {},
          onOpenPlaza() {},
          onDraftChange() {},
          onCancel() {},
          onComplete() {},
        }),
      ),
    );
  });

  const dialog = container.querySelector('[role="dialog"][data-testid="jiuwenmate-template-skill-error-dialog"]');
  assert.ok(dialog);
  assert.match(dialog.textContent, /技能广场搜索暂不可用/);
  assert.equal(container.querySelector('[data-testid="jiuwenmate-template-skill-inline-error"]'), null);

  await act(async () => root.unmount());
  dom.window.close();
});

test('an empty keyword recommendation stays hidden and does not open an error dialog', async () => {
  const dom = new JSDOM('<!doctype html><html><head></head><body></body></html>', { pretendToBeVisual: true });
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  Object.defineProperty(globalThis, 'document', { configurable: true, value: dom.window.document });
  Object.defineProperty(globalThis, 'navigator', { configurable: true, value: dom.window.navigator });
  Object.defineProperty(globalThis, 'HTMLElement', { configurable: true, value: dom.window.HTMLElement });
  Object.defineProperty(globalThis, 'Node', { configurable: true, value: dom.window.Node });
  Object.defineProperty(globalThis, 'IS_REACT_ACT_ENVIRONMENT', { configurable: true, value: true });

  const container = dom.window.document.createElement('div');
  dom.window.document.body.append(container);
  const root = createRoot(container);
  await act(async () => {
    root.render(
      React.createElement(
        I18nextProvider,
        { i18n },
        React.createElement(ColleagueCreatePage, {
          step: 'refine',
          source: 'template',
          detailTemplateId: null,
          customDialogOpen: false,
          draft: {
            name: '英语表达教练',
            description: '改进英文表达。',
            tags: [],
            category: 'education',
            capabilities: { model: '', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
          },
          modelCatalog: { models: [], defaultModelId: '' },
          resourceCatalog: { skills: [], connectors: [], plugins: [], knowledge: [] },
          resourceCatalogReady: true,
          missingTemplateCapabilities: { skills: [], connectors: [], plugins: [], knowledge: [] },
          skillRecommendations: { status: 'ready', local: [], plaza: [], error: null },
          onDismissSkillRecommendationError() {},
          onInstallSkill: async () => 'unused',
          submitting: false,
          onOpenTemplateDetail() {},
          onCloseTemplateDetail() {},
          onChooseTemplate() {},
          onOpenCustom() {},
          onCloseCustom() {},
          onConfirmCustom() {},
          onOpenPlaza() {},
          onDraftChange() {},
          onCancel() {},
          onComplete() {},
        }),
      ),
    );
  });

  assert.equal(container.querySelector('[role="dialog"]'), null);
  assert.equal(container.querySelector('[data-testid="jiuwenmate-template-skill-recommendations"]'), null);
  assert.equal(container.textContent.includes('技能广场暂未找到匹配的技能'), false);

  await act(async () => root.unmount());
  dom.window.close();
});
