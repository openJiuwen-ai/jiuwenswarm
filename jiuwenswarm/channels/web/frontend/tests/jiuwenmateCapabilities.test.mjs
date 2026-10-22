import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildTemplateSkillSearchQuery,
  templateSkillSearchQueries,
  recommendInstalledTemplateSkills,
  recommendMarketplaceTemplateSkills,
  splitAvailableColleagueCapabilities,
  selectGeneratedColleagueCapabilities,
  installedExpertSkillNames,
  withInstalledExpertSkills,
} from '../node_modules/.cache/jiuwenmate-capabilities/colleagueCapabilities.js';

const catalog = {
  skills: [{ name: 'report-writer' }],
  connectors: [{ name: 'feishu-docs', label: '飞书文档' }],
  plugins: null,
  knowledge: [{ name: 'product-manual' }],
};

test('an installed expert keeps its own skills as the employee default', () => {
  assert.deepEqual(
    installedExpertSkillNames([
      { id: 'risk-based-code-review', name: '风险评审' },
      { id: '', name: '变更说明' },
      { id: 'risk-based-code-review', name: '重复' },
    ]),
    ['risk-based-code-review', '变更说明'],
  );

  const split = splitAvailableColleagueCapabilities(
    {
      model: '',
      skills: ['risk-based-code-review', '变更说明', 'report-writer'],
      connectors: [],
      plugins: [],
      knowledge: [],
      starterPrompts: [],
    },
    catalog,
  );
  const result = withInstalledExpertSkills(['risk-based-code-review', '变更说明', 'report-writer'], split);

  assert.deepEqual(result.available.skills, ['report-writer', 'risk-based-code-review']);
  assert.deepEqual(result.missing.skills, ['变更说明']);
});

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

test('template plaza query uses curated skill terms instead of the role title', () => {
  assert.deepEqual(
    templateSkillSearchQueries({
      name: '英语表达教练',
      skills: [{ name: '问题定位' }, { name: '自然改写' }],
      skillSearchTerms: ['英文润色', '语法检查', '英语写作'],
    }),
    ['英文润色', '语法检查', '英语写作'],
  );
  assert.deepEqual(
    templateSkillSearchQueries({
      name: '系统架构工程师',
      coreAbilities: [{ title: '依赖梳理' }],
    }),
    ['系统架构工程师 依赖梳理'],
  );
  assert.equal(
    buildTemplateSkillSearchQuery({
      name: '英语表达教练',
      skillSearchTerms: ['英文润色', '语法检查'],
    }),
    '英文润色 语法检查',
  );
});

test('installed template skill recommendations rank matching metadata and omit unrelated skills', () => {
  assert.deepEqual(
    recommendInstalledTemplateSkills(
      ['架构', 'architecture', '代码审查', 'code review'],
      [
        { name: 'architecture-notes', label: '架构记录', description: 'Architecture documentation' },
        { name: 'code-review', label: '代码审查', description: 'Architecture code review changes' },
        { name: 'team-review', label: '团队评审', description: 'Architecture team review', skillType: 'swarm_skill' },
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
            source: 'teamskillshub',
            name: 'team-architecture-review',
            display_name: '团队架构评审',
            identifier: 'asset-team',
            description: 'Review architecture as a team',
            is_team_skill: true,
            plugin_type: 'swarmskill',
          },
          {
            source: 'clawhub',
            name: 'swarm-notes',
            display_name: '群体笔记',
            identifier: 'swarm-notes',
            skill_type: 'swarm_skill',
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

test('marketplace recommendations drop skills that do not contain a curated template term', () => {
  assert.deepEqual(
    recommendMarketplaceTemplateSkills(
      {
        items: [
          {
            source: 'teamskillshub',
            name: 'fitness-coach',
            display_name: '健身教练',
            identifier: 'fitness-coach',
            description: '制定训练计划',
          },
          {
            source: 'clawhub',
            name: 'tech-fund',
            display_name: '科技基金',
            identifier: 'tech-fund',
            description: '跟踪科技行业基金表现',
          },
          {
            source: 'clawhub',
            name: 'writing-helper',
            display_name: '写作助手',
            identifier: 'writing-helper',
            description: '协助完成一般写作',
          },
          {
            source: 'teamskillshub',
            name: 'grammar-fix',
            display_name: '语法纠错',
            identifier: 'grammar-fix',
            description: '标出句子中的语法问题',
          },
          {
            source: 'teamskillshub',
            name: 'english-editing',
            display_name: '英语写作批改',
            identifier: 'english-editing',
            description: '检查语法并给出改写',
          },
        ],
      },
      [],
      5,
      ['英文润色', '语法检查', '英语写作', 'grammar correction'],
    ),
    [
      {
        source: 'teamskillshub',
        name: 'grammar-fix',
        display_name: '语法纠错',
        identifier: 'grammar-fix',
        description: '标出句子中的语法问题',
      },
      {
        source: 'teamskillshub',
        name: 'english-editing',
        display_name: '英语写作批改',
        identifier: 'english-editing',
        description: '检查语法并给出改写',
      },
    ],
  );
});
