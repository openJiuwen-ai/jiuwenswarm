import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { initReactI18next } from 'react-i18next';

import { ColleagueProfilePage } from '../node_modules/.cache/jiuwenmate-profile/ColleagueProfilePage.js';

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false } });

const colleague = {
  id: 'agent-1',
  name: '小九',
  description: '通用办公助理',
  tags: ['效率提升'],
  category: 'custom',
  capabilities: {
    model: 'deepseek#0',
    skills: ['old-skill'],
    connectors: [],
    plugins: [],
    knowledge: [],
    starterPrompts: [],
  },
  imConfigured: false,
  disabled: false,
  joinedAt: '2026-10-14T08:00:00+00:00',
};

const noop = () => undefined;

const catalog = {
  skills: [{ name: 'report-writer', label: '报告' }],
  connectors: [{ name: 'feishu', label: '飞书' }],
  plugins: [],
  ready: true,
};

function renderProfile(extra) {
  return renderToStaticMarkup(
    React.createElement(ColleagueProfilePage, {
      colleague,
      resourceCatalog: catalog,
      onSaveSettings: noop,
      onOpenOffice: noop,
      onChat: noop,
      onConfigureIm: noop,
      onToggleDisabled: noop,
      onDelete: noop,
      ...extra,
    }),
  );
}

test('skills can be changed on the skills page without the pencil', () => {
  const markup = renderProfile({ initialSection: 'skills' });
  assert.match(markup, /data-testid="jiuwenmate-profile-detail"[^>]*data-variant="skills"/);
  assert.match(markup, /添加技能/);
  assert.match(markup, /old-skill/);
  assert.doesNotMatch(markup, /data-testid="jiuwenmate-profile-edit"/);
  assert.doesNotMatch(markup, /AI 员工名称/);
});

test('the pencil edits the current page and stays there', () => {
  const overview = renderProfile({ initialSection: 'overview', initialEditing: true });
  assert.match(overview, /aria-current="true"[^>]*data-variant="overview"/);
  assert.match(overview, /aria-label="编辑人设"/);
  assert.match(overview, /data-testid="jiuwenmate-profile-name-input"/);
  assert.doesNotMatch(overview, /data-testid="jiuwenmate-profile-detail"/);

  const model = renderProfile({
    initialSection: 'model',
    initialEditing: true,
    modelCatalog: { models: [{ id: 'deepseek#0', label: 'DeepSeek', provider: 'DeepSeek' }], defaultModelId: 'deepseek#0', defaultModelLabel: 'DeepSeek' },
  });
  assert.match(model, /aria-current="true"[^>]*data-variant="model"/);
  assert.match(model, /aria-label="编辑模型"/);
  assert.match(model, /data-testid="jiuwenmate-profile-detail"[^>]*data-variant="model"/);
  assert.match(model, /data-testid="jiuwenmate-profile-model-select"/);
});
