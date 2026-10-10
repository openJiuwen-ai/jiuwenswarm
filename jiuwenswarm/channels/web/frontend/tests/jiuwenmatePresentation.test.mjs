import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { initReactI18next } from 'react-i18next';

import {
  JIUWENMATE_CHANNELS,
  JIUWENMATE_DAILY_ACTIVITY,
  JIUWENMATE_TASKS,
  JIUWENMATE_TEMPLATES,
  JIUWENMATE_TRACE_ROWS,
} from '../node_modules/.cache/jiuwenmate-presentation/mockData.js';
import { ColleagueCard } from '../node_modules/.cache/jiuwenmate-presentation/components/ColleagueCard.js';
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
  assert.ok(JIUWENMATE_TEMPLATES.every((item) => item.coreAbilities.length === 3));
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
