import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildTemplateSkillSearchQuery,
  recommendInstalledTemplateSkills,
  recommendMarketplaceTemplateSkills,
  splitAvailableColleagueCapabilities,
  selectGeneratedColleagueCapabilities,
} from '../node_modules/.cache/jiuwenmate-capabilities/colleagueCapabilities.js';

const catalog = {
  skills: [{ name: 'report-writer' }],
  connectors: [{ name: 'feishu-docs', label: '飞书文档' }],
  plugins: null,
  knowledge: [{ name: 'product-manual' }],
};

test('template capabilities are split against the live installed catalog', () => {
  const result = splitAvailableColleagueCapabilities(
    {
      model: '',
      skills: ['report-writer', 'context-analyst'],
      connectors: ['飞书文档', 'survey'],
      plugins: ['web-search'],
      knowledge: ['product-manual'],
      starterPrompts: [],
    },
    catalog,
  );

  assert.deepEqual(result.available, {
    skills: ['report-writer'],
    connectors: ['feishu-docs'],
    plugins: [],
    knowledge: ['product-manual'],
  });
  assert.deepEqual(result.missing, {
    skills: ['context-analyst'],
    connectors: ['survey'],
    plugins: ['web-search'],
    knowledge: [],
  });
});

test('smart generation selects only resources currently available to publish', () => {
  assert.deepEqual(
    selectGeneratedColleagueCapabilities(
      { skills: ['context-analyst', 'report-writer'], connectors: ['survey'], plugins: [], knowledge: [] },
      catalog,
    ),
    { skills: ['report-writer'], connectors: [], plugins: [], knowledge: [] },
  );
});

test('template plaza query uses the template name, core abilities, and curated skill terms', () => {
  assert.equal(
    buildTemplateSkillSearchQuery({
      name: '系统架构工程师',
      coreAbilities: [{ title: '依赖梳理' }, { title: '方案评审' }],
      skillSearchTerms: ['backend architecture', '代码审查'],
    }),
    '系统架构工程师 依赖梳理 方案评审 backend architecture 代码审查',
  );
});

test('installed template skill recommendations rank matching metadata and omit unrelated skills', () => {
  assert.deepEqual(
    recommendInstalledTemplateSkills(
      ['架构', 'architecture', '代码审查', 'code review'],
      [
        { name: 'architecture-notes', label: '架构记录', description: 'Architecture documentation' },
        { name: 'code-review', label: '代码审查', description: 'Architecture code review changes' },
        { name: 'meeting-minutes', label: '会议纪要', description: 'Summarize meetings' },
      ],
    ),
    [
      { name: 'code-review', label: '代码审查', description: 'Architecture code review changes' },
      { name: 'architecture-notes', label: '架构记录', description: 'Architecture documentation' },
    ],
  );
});

test('marketplace recommendations exclude installed, duplicate, and non-installable results', () => {
  assert.deepEqual(
    recommendMarketplaceTemplateSkills(
      {
        items: [
          {
            source: 'teamskillshub',
            name: 'architecture-review',
            display_name: '架构评审',
            identifier: 'asset-1',
            description: 'Review service architecture',
          },
          {
            source: 'clawhub',
            name: 'already-installed',
            display_name: '已安装技能',
            identifier: 'already-installed',
          },
          {
            source: 'clawhub',
            name: 'architecture-review',
            display_name: '架构评审',
            identifier: 'architecture-review',
          },
          {
            source: 'unknown',
            name: 'unsupported',
            display_name: '不支持的来源',
            identifier: 'unsupported',
          },
          {
            source: 'clawhub',
            name: 'skill-bundle',
            display_name: '技能包',
            identifier: 'skill-bundle',
            skill_type: 'skillpack',
          },
        ],
      },
      ['already-installed'],
    ),
    [
      {
        source: 'teamskillshub',
        name: 'architecture-review',
        display_name: '架构评审',
        identifier: 'asset-1',
        description: 'Review service architecture',
      },
    ],
  );
});
