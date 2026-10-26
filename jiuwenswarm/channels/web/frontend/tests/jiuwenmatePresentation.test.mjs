import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React, { act } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createRoot } from 'react-dom/client';
import { initReactI18next } from 'react-i18next';
import { JSDOM } from 'jsdom';

import {
  JIUWENMATE_CHANNELS,
  JIUWENMATE_DAILY_ACTIVITY,
  JIUWENMATE_TASKS,
  JIUWENMATE_TEMPLATES,
  JIUWENMATE_TRACE_ROWS,
} from '../node_modules/.cache/jiuwenmate-presentation/mockData.js';
import { CUSTOM_AVATAR_HUES, pickAvatarHue } from '../node_modules/.cache/jiuwenmate-presentation/avatarHue.js';
import { ColleagueAvatar } from '../node_modules/.cache/jiuwenmate-presentation/components/ColleagueAvatar.js';
import { ColleagueCard } from '../node_modules/.cache/jiuwenmate-presentation/components/ColleagueCard.js';
import { ConversationComposer } from '../node_modules/.cache/jiuwenmate-presentation/components/ConversationComposer.js';
import { ActivityHeatmap, HeatmapLegend } from '../node_modules/.cache/jiuwenmate-presentation/components/ActivityHeatmap.js';
import { yearHeatmap } from '../node_modules/.cache/jiuwenmate-presentation/officeState.js';

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false } });

test('demo collections expose readable stable IDs without duplicates', () => {
  for (const collection of [JIUWENMATE_TEMPLATES, JIUWENMATE_CHANNELS, JIUWENMATE_TASKS, JIUWENMATE_TRACE_ROWS]) {
    const ids = collection.map((item) => item.id);
    assert.equal(new Set(ids).size, ids.length);
    assert.ok(ids.every((id) => /^[a-z][a-z0-9-]+$/.test(id)));
  }
});

test('templates follow the eight designed roles with their category art', () => {
  assert.deepEqual(
    JIUWENMATE_TEMPLATES.map((item) => item.name),
    ['英语表达教练', '系统架构工程师', '基本面研究员', '连载故事策划', '个人理财专家', '案件整理顾问', '视频分镜师', '研究写作顾问'],
  );
  assert.equal(new Set(JIUWENMATE_TEMPLATES.map((item) => item.category)).size, JIUWENMATE_TEMPLATES.length);
  assert.ok(JIUWENMATE_TEMPLATES.every((item) => item.persona.trim().length > 0 && item.skills.length === 3));
  assert.ok(JIUWENMATE_TEMPLATES.every((item) => item.tools.length === 0 && item.rails.length === 0 && item.mcps.length === 0));
  assert.ok(JIUWENMATE_TEMPLATES.every((item) => item.capabilities.connectors.length === 0 && item.capabilities.plugins.length === 0));
});

test('template skills are discovered from installed skills and the plaza instead of placeholder IDs', () => {
  assert.ok(JIUWENMATE_TEMPLATES.every((item) => item.capabilities.skills.length === 0));
  assert.ok(JIUWENMATE_TEMPLATES.every((item) => (item.skillSearchTerms?.length ?? 0) >= 3));
});

test('a custom colleague takes a different avatar hue from the default colleague', () => {
  const markup = (colleague) => renderToStaticMarkup(React.createElement(ColleagueAvatar, { colleague }));
  const hueOf = (html) => html.match(/data-hue="(\d+)"/)?.[1];
  const original = markup({ id: 'mate-default', name: '小九', category: 'custom', isDefault: true });
  const first = markup({ id: 'mate-a', name: '甲', category: 'custom' });
  const renamed = markup({ id: 'mate-a', name: '完全不同的名字', category: 'custom' });
  const second = markup({ id: 'mate-b', name: '乙', category: 'custom' });
  const namedOnly = markup({ name: '甲', category: 'custom' });
  const chosen = markup({ id: 'mate-b', name: '乙', category: 'custom', avatarHue: 110 });
  const uploaded = markup({
    id: 'mate-a',
    name: '甲',
    category: 'custom',
    avatarUrl: 'https://example/a.png',
    avatarHue: 110,
  });
  assert.doesNotMatch(original, /data-hue=/);
  assert.doesNotMatch(namedOnly, /data-hue=/);
  assert.doesNotMatch(uploaded, /data-hue=/);
  assert.equal(hueOf(first), hueOf(renamed));
  assert.notEqual(hueOf(first), hueOf(second));
  assert.equal(hueOf(chosen), '110');
  assert.equal(pickAvatarHue(), CUSTOM_AVATAR_HUES[0]);
  assert.equal(pickAvatarHue([55, 110, 155]), 190);
  assert.equal(pickAvatarHue([155, 155, 110]), 55);
  const card = renderToStaticMarkup(
    React.createElement(ColleagueCard, {
      colleague: {
        id: 'mate-a',
        name: '甲',
        description: '整理日程',
        tags: [],
        category: 'custom',
        capabilities: { model: '', skills: [], connectors: [], plugins: [], knowledge: [], starterPrompts: [] },
      },
    }),
  );
  assert.match(card, /data-testid="jiuwenmate-colleague-card"[^>]*data-hue="\d+"/);
});

test('colleague card renders the identity, badge and category art', () => {
  const template = JIUWENMATE_TEMPLATES[0];
  const markup = renderToStaticMarkup(
    React.createElement(ColleagueCard, { colleague: template, selected: true, badge: { label: '未配置IM', tone: 'warning' } }),
  );

  assert.match(markup, /data-testid="jiuwenmate-colleague-card"/);
  assert.match(markup, new RegExp(`data-variant="${template.id}"`));
  assert.match(markup, new RegExp(`data-category="${template.category}"`));
  assert.match(markup, new RegExp(template.name));
  assert.match(markup, new RegExp(template.tags[0]));
  assert.match(markup, /未配置IM/);
  assert.match(markup, /data-testid="jiuwenmate-colleague-avatar"/);
});

test('colleague tags paint above the portrait artwork when the tag list wraps', () => {
  const stylesheet = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');
  const markup = renderToStaticMarkup(
    React.createElement(ColleagueCard, {
      colleague: {
        ...JIUWENMATE_TEMPLATES[0],
        tags: ['研发管理', '质量评估', '风险控制', '流程治理', '合规审查'],
      },
    }),
  );
  const dom = new JSDOM(`<style>${stylesheet}</style>${markup}`);
  const tags = dom.window.document.querySelector('.jiuwenmate-colleague-card__tags');
  const artwork = dom.window.document.querySelector('.jiuwenmate-colleague-card__art');

  assert.equal(dom.window.getComputedStyle(tags).position, 'relative');
  assert.equal(dom.window.getComputedStyle(tags).zIndex, '1');
  assert.equal(dom.window.getComputedStyle(artwork).position, 'absolute');
  dom.window.close();
});

test('colleague card exposes the designed hover actions without nesting buttons', () => {
  const markup = renderToStaticMarkup(
    React.createElement(ColleagueCard, {
      colleague: JIUWENMATE_TEMPLATES[1],
      actions: [
        { label: '去对话', onClick() {} },
        { label: '管理', primary: true, onClick() {} },
      ],
    }),
  );

  assert.match(markup, /jiuwenmate-colleague-card__actions/);
  assert.match(markup, /去对话/);
  assert.match(markup, /jiuwenmate-colleague-card__action--primary">管理/);
  const selectButton = markup.match(/<button[^>]*jiuwenmate-colleague-card__select[^>]*>[\s\S]*?<\/button>/)?.[0] ?? '';
  assert.equal((selectButton.match(/<button/g) ?? []).length, 1);
});

test('year heatmap renders weekday labels, month labels and the design legend', () => {
  const heatmap = yearHeatmap(2026, new Date(2026, 9, 12), JIUWENMATE_DAILY_ACTIVITY);
  const markup = renderToStaticMarkup(
    React.createElement(React.Fragment, null,
      React.createElement(ActivityHeatmap, { heatmap, hoveredCellId: '2026-01-16', onHover() {} }),
      React.createElement(HeatmapLegend),
    ),
  );

  assert.match(markup, /jiuwenmate-heatmap__months/);
  assert.match(markup, /周一/);
  assert.match(markup, /周三/);
  assert.match(markup, /周五/);
  assert.match(markup, /11月/);
  assert.match(markup, />少</);
  assert.match(markup, />多</);
  assert.match(markup, /2026\/01\/16/);
  assert.match(markup, /任务数：112/);
  assert.match(markup, /工作时长：3.1h/);
});

test('private chat composer reflects the saved employee permission profile', () => {
  const markup = renderToStaticMarkup(
    React.createElement(ConversationComposer, {
      value: '',
      onChange() {},
      permissionProfile: 'automatic',
      onPermissionProfileChange() {},
      modelCatalog: {
        models: [{ id: 'deepseek-flash#0', label: 'DeepSeek Flash', provider: 'deepseek' }],
        defaultModelId: 'other',
        defaultModelLabel: '其他模型',
      },
      modelId: 'deepseek-flash#0',
    }),
  );

  assert.match(markup, /data-testid="jiuwenmate-composer-model"/);
  assert.match(markup, /DeepSeek Flash/);
  assert.match(markup, /data-testid="jiuwenmate-composer-permission-trigger"[^>]*data-variant="automatic"/);
  assert.match(markup, /智能审批/);
  assert.doesNotMatch(markup, />Auto</);
  assert.doesNotMatch(markup, /jiuwenmate-composer__usage/);
});

test('private chat composer can change the employee permission profile', async () => {
  const dom = new JSDOM('<!doctype html><div id="root"></div>', { url: 'http://localhost' });
  const previous = {
    window: globalThis.window,
    document: globalThis.document,
    actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
  };
  globalThis.window = dom.window;
  globalThis.document = dom.window.document;
  globalThis.IS_REACT_ACT_ENVIRONMENT = true;
  const container = dom.window.document.getElementById('root');
  const root = createRoot(container);
  const changes = [];
  try {
    await act(async () => {
      root.render(React.createElement(ConversationComposer, {
        value: '',
        onChange() {},
        permissionProfile: 'default',
        onPermissionProfileChange: (profile) => changes.push(profile),
        modelCatalog: { models: [], defaultModelId: '', defaultModelLabel: '' },
        modelId: '',
      }));
    });

    await act(async () => {
      container.querySelector('[data-testid="jiuwenmate-composer-permission-trigger"]').click();
    });
    const automaticOption = container.querySelector('[data-testid="jiuwenmate-composer-permission-option"][data-variant="automatic"]');
    assert.ok(automaticOption);
    await act(async () => automaticOption.click());

    assert.deepEqual(changes, ['automatic']);
  } finally {
    await act(async () => root.unmount());
    dom.window.close();
    globalThis.window = previous.window;
    globalThis.document = previous.document;
    globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
  }
});
