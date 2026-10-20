import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import i18n from 'i18next';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { initReactI18next } from 'react-i18next';

import { ColleagueProfilePage } from '../node_modules/.cache/jiuwenmate-profile/pages/ColleagueProfilePage.js';
import { ScheduleBoard } from '../node_modules/.cache/jiuwenmate-profile/schedules/ScheduleBoard.js';

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const profileStyles = readFileSync(new URL('../src/features/jiuwenmate/jiuwenmate.css', import.meta.url), 'utf8');
await i18n.use(initReactI18next).init({ lng: 'zh', resources: { zh: { translation: zh } }, interpolation: { escapeValue: false } });

const colleague = {
  id: 'agent-1',
  name: '小九',
  description: '通用办公助理',
  persona: '角色：群客服智能体\n规则：准确回答问题。',
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
  skills: [{ name: 'report-writer', label: '报告', description: '整理周报' }],
  connectors: [{ name: 'feishu', label: '飞书', description: '' }],
  plugins: [],
  knowledge: [{ name: 'topics/description.md', label: 'topics/description.md', description: '' }],
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

test('the overview hides quick IM setup after the channel is configured', () => {
  const channels = renderProfile({
    initialSection: 'channels',
    colleague: { ...colleague, imConfigured: true },
  });
  assert.match(channels, /data-variant="channels"/);
  assert.match(channels, /已授权/);
  assert.match(channels, /data-testid="jiuwenmate-profile-im-modify"/);
  assert.match(channels, />修改</);

  const overview = renderProfile({ colleague: { ...colleague, imConfigured: true } });
  assert.doesNotMatch(overview, /data-testid="jiuwenmate-profile-quick-im"/);
});

test('the overview shows quick IM setup when no channel is configured', () => {
  const overview = renderProfile({ colleague: { ...colleague, imConfigured: false } });
  assert.match(overview, /data-testid="jiuwenmate-profile-quick-im"/);
});

test('skills can be changed on the skills page without the pencil', () => {
  const markup = renderProfile({ initialSection: 'skills' });
  assert.match(markup, /data-testid="jiuwenmate-profile-detail"[^>]*data-variant="skills"/);
  assert.match(markup, /添加技能/);
  assert.match(markup, /old-skill/);
  assert.match(markup, /class="jiuwenmate-profile__resource-grid"/);
  assert.match(markup, /data-testid="jiuwenmate-profile-resource-card"/);
  assert.match(markup, /暂无描述/);
  assert.doesNotMatch(markup, /jiuwenmate-profile__resource-picker/);
  assert.doesNotMatch(markup, /data-testid="jiuwenmate-profile-edit"/);
  assert.doesNotMatch(markup, /AI 员工名称/);
});

test('configuration pages use the design-specific persona, model, and resource layouts', () => {
  const persona = renderProfile({ initialSection: 'persona' });
  assert.match(persona, /class="jiuwenmate-profile__persona-editor"/);
  assert.match(persona, /data-section="persona"/);
  assert.match(persona, /角色：群客服智能体/);
  assert.doesNotMatch(persona, /value="通用办公助理"/);
  assert.match(profileStyles, /data-section\]:not\(\[data-section='overview'\]\) > \.jiuwenmate-profile__header/);
  assert.match(profileStyles, /\.jiuwenmate-profile__persona-editor textarea[\s\S]*?min-height: 500px/);

  const model = renderProfile({
    initialSection: 'model',
    modelCatalog: { models: [{ id: 'deepseek#0', label: 'DeepSeek V3', provider: 'DeepSeek' }], defaultModelId: '', defaultModelLabel: '' },
  });
  assert.match(model, /class="jiuwenmate-profile__model-list"/);
  assert.match(model, /class="jiuwenmate-profile__model-icon"/);
  assert.match(model, /选择模型/);
  assert.doesNotMatch(model, /name="jiuwenmate-profile-model"[^>]*disabled/);
  assert.match(model, /jiuwenmate-profile__save-button/);
  assert.doesNotMatch(model, /data-testid="jiuwenmate-profile-edit"/);
  assert.match(model, /DeepSeek V3/);

  for (const section of ['skills', 'connectors', 'plugins']) {
    const markup = renderProfile({
      initialSection: section,
      colleague: { ...colleague, capabilities: { ...colleague.capabilities, [section]: ['configured-item'] } },
    });
    assert.match(markup, /class="jiuwenmate-profile__resource-grid"/);
  }

  const knowledgeEmpty = renderProfile({ initialSection: 'knowledge' });
  assert.match(knowledgeEmpty, /class="jiuwenmate-profile__knowledge-empty"/);
  const knowledgePopulated = renderProfile({
    initialSection: 'knowledge',
    colleague: { ...colleague, capabilities: { ...colleague.capabilities, knowledge: ['topics/description.md'] } },
  });
  assert.match(knowledgePopulated, /class="jiuwenmate-profile__knowledge-list"/);
});

test('schedules can be created on the schedules page', () => {
  const markup = renderProfile({
    initialSection: 'schedules',
    schedules: [
      {
        id: 'plan-1',
        agentId: 'agent-1',
        name: '晨报',
        prompt: '汇总昨日事项',
        cronExpr: '0 9 * * *',
        timezone: 'Asia/Shanghai',
        enabled: true,
        nextFireAt: '2026-10-18T01:00:00+00:00',
        scheduleKind: 'cron',
        intervalSeconds: null,
        runAt: null,
        startsAt: null,
        endsAt: null,
        conversationId: 'schedule:plan-1',
        runCount: 0,
        lastStatus: 'running',
      },
    ],
    onCreateSchedule: noop,
    onSetScheduleEnabled: noop,
    onDeleteSchedule: noop,
  });
  assert.match(markup, /data-variant="schedules"/);
  assert.match(markup, /晨报/);
  assert.match(markup, /每天 9:00/);
  assert.match(markup, /添加/);
  assert.match(markup, /jiuwenmate-schedule-section__bar[\s\S]*?<h2>定时任务<\/h2>[\s\S]*?data-testid="jiuwenmate-schedule-add"/);
  assert.match(markup, /data-testid="jiuwenmate-schedule-add"/);
  assert.match(markup, /class="jiuwenmate-outline-button jiuwenmate-schedule-add"[^>]*data-testid="jiuwenmate-schedule-add"/);
  assert.match(markup, /lucide-plus/);
  assert.match(markup, /class="jiuwenmate-schedule-card"/);
  assert.match(markup, /jiuwenmate-schedule-card__actions/);
  assert.match(markup, /role="switch"/);
  assert.match(markup, /class="jiuwenmate-switch__thumb"/);
});

test('schedule board keeps the executor avatar color selected for the colleague', () => {
  const markup = renderToStaticMarkup(
    React.createElement(ScheduleBoard, {
      schedules: [
        {
          id: 'plan-1', agentId: 'agent-1', name: '晨报', prompt: '汇总昨日事项', cronExpr: '0 9 * * *', timezone: 'Asia/Shanghai',
          enabled: true, nextFireAt: '2026-10-18T01:00:00+00:00', scheduleKind: 'cron', intervalSeconds: null, runAt: null,
          startsAt: null, endsAt: null, conversationId: 'schedule:plan-1', runCount: 0, lastStatus: 'running',
        },
      ],
      colleagues: [{ id: 'agent-1', name: '小九', category: 'custom', avatarHue: 230 }],
      onCreate: noop,
      onUpdate: noop,
      onSetEnabled: noop,
      onDelete: noop,
      onLoadHistory: async () => ({ plan: null, runs: [] }),
    }),
  );
  assert.match(markup, /data-hue="230"[^>]*data-testid="jiuwenmate-colleague-avatar"/);
  assert.match(markup, /--jm-avatar-hue:230deg/);
});

test('schedule board keeps the default executor portrait unfiltered', () => {
  const markup = renderToStaticMarkup(
    React.createElement(ScheduleBoard, {
      schedules: [
        {
          id: 'plan-default', agentId: 'default-agent', name: '晨报', prompt: '汇总昨日事项', cronExpr: '0 9 * * *', timezone: 'Asia/Shanghai',
          enabled: true, nextFireAt: '2026-10-18T01:00:00+00:00', scheduleKind: 'cron', intervalSeconds: null, runAt: null,
          startsAt: null, endsAt: null, conversationId: 'schedule:plan-default', runCount: 0, lastStatus: 'running',
        },
      ],
      colleagues: [{ id: 'default-agent', name: '小九', category: 'custom', isDefault: true }],
      onCreate: noop,
      onUpdate: noop,
      onSetEnabled: noop,
      onDelete: noop,
      onLoadHistory: async () => ({ plan: null, runs: [] }),
    }),
  );
  assert.doesNotMatch(markup, /data-hue=/);
});

test('the overview shows the employee activity instead of an empty log', () => {
  const markup = renderProfile({
    today: new Date('2026-10-19T12:00:00+08:00'),
    activity: {
      agentId: 'agent-1',
      calls: 1,
      tokens: 12,
      tasks: 2,
      replies: 1,
      failures: 1,
      companionDays: 6,
      lastResponseAt: '2026-10-16T10:01:00+00:00',
      days: [{ date: '2026-10-16', tasks: 2, minutes: 1, calls: 1, tokens: 12 }],
      kinds: [{ date: '2026-10-16', conversation: 1, channel: 0, schedule: 1 }],
      history: [
        {
          requestId: 'req-plan',
          title: '喝水提醒',
          kind: 'schedule',
          channel: 'web',
          status: 'completed',
          tokens: 12,
          updatedAt: '2026-10-16T03:01:00+00:00',
          startedAt: '2026-10-16T03:00:00+00:00',
        },
      ],
      hours: [],
      channels: [{ channel: 'web', calls: 1, tokens: 12, tasks: 2, replies: 1, failures: 1 }],
      groups: [],
    },
  });
  assert.match(markup, /data-testid="jiuwenmate-profile-metrics"/);
  assert.match(markup, /data-level="1"[^>]*data-variant="2026-10-16"/);
  assert.match(markup, /data-testid="jiuwenmate-profile-task-count"[\s\S]*?<strong>2<\/strong>/);
  assert.match(markup, /data-kind="schedule"[\s\S]*?<strong>1<\/strong>/);
  assert.match(markup, /data-testid="jiuwenmate-profile-history-row"[\s\S]*?喝水提醒/);
  assert.match(markup, /已完成/);
  assert.doesNotMatch(markup, /暂无执行记录/);
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
  assert.doesNotMatch(model, /aria-label="编辑模型"/);
  assert.match(model, /data-testid="jiuwenmate-profile-detail"[^>]*data-variant="model"/);
  assert.match(model, /type="radio"/);
});

test('knowledge uses the compact page list without exposing the office directory', () => {
  const markup = renderProfile({
    initialSection: 'knowledge',
    colleague: {
      ...colleague,
      workspaceDir: '/tmp/office',
      capabilities: { ...colleague.capabilities, knowledge: ['topics/description.md'] },
    },
  });
  assert.match(markup, /data-variant="knowledge"/);
  assert.match(markup, /添加知识/);
  assert.match(markup, /topics\/description\.md/);
  assert.match(markup, /class="jiuwenmate-profile__knowledge-list"/);
  assert.doesNotMatch(markup, /\/tmp\/office/);
});

test('an empty knowledge catalog tells the user to publish a page first', () => {
  const markup = renderProfile({
    initialSection: 'knowledge',
    resourceCatalog: { ...catalog, knowledge: [] },
  });
  assert.match(markup, /添加知识/);
  assert.match(markup, /没有可装载的已发布知识页面/);
  assert.match(markup, /左侧「知识」/);
});
