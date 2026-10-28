import assert from 'node:assert/strict';
import test from 'node:test';

import {
  assignInstalledExpertHues,
  installedExpertTemplateId,
  installedExpertToTemplate,
  isInstalledExpertHue,
  loadInstalledExpertTemplates,
  templateAlreadyCreated,
} from '../node_modules/.cache/jiuwenmate-installed-experts/installedExperts.js';

const installed = {
  id: 'hub-reviewer',
  runtimePackageName: 'code-reviewer',
  displayName: '代码评审',
  description: '评审变更并指出风险。',
  installed: true,
  tags: [{ id: 'dev', label: '研发工程' }],
};

test('an installed expert uses the custom bee and keeps its expert configuration', () => {
  const template = installedExpertToTemplate(installed, {
    persona: '你是代码评审。只讨论这次变更。',
    skills: [{ id: 'review', name: '变更评审', description: '指出风险。' }],
    tools: [{ id: 'diff', name: '差异阅读', description: '读取差异。' }],
    rails: [{ id: 'scope', name: '范围边界', description: '不扩展需求。' }],
    mcps: [{ id: 'repo', name: '代码仓库', description: '读取仓库。' }],
    suggestedPrompts: ['评审这次改动。'],
  });

  assert.ok(template);
  assert.equal(template.id, installedExpertTemplateId('code-reviewer'));
  assert.equal(template.category, 'custom');
  assert.equal(template.avatarUrl, undefined);
  assert.equal(template.persona, '你是代码评审。只讨论这次变更。');
  assert.deepEqual(template.tags, ['研发工程']);
  assert.deepEqual(template.skills.map((item) => item.name), ['变更评审']);
  assert.deepEqual(template.tools.map((item) => item.name), ['差异阅读']);
  assert.deepEqual(template.rails.map((item) => item.name), ['范围边界']);
  assert.deepEqual(template.mcps.map((item) => item.name), ['代码仓库']);
  assert.deepEqual(template.capabilities.starterPrompts, ['评审这次改动。']);
  assert.deepEqual(template.capabilities.skills, ['review']);
  assert.equal(isInstalledExpertHue(template.avatarHue), true);
  assert.equal(installedExpertToTemplate(installed)?.avatarHue, template.avatarHue);
});

test('uninstalled experts and duplicate packages stay off the create page', async () => {
  assert.equal(installedExpertToTemplate({ ...installed, installed: false }), null);
  const templates = await loadInstalledExpertTemplates(
    async () => [
      { ...installed, installed: false },
      installed,
      { ...installed, id: 'again' },
      {
        id: 'writer',
        runtimePackageName: 'research-writer',
        displayName: '研究写作',
        description: '整理论证。',
        installed: true,
        tags: [],
      },
    ],
    async (id) => {
      if (id === 'writer') throw new Error('detail unavailable');
      return { persona: '你是代码评审。' };
    },
  );

  assert.deepEqual(templates.map((item) => item.installedPackageName), ['code-reviewer', 'research-writer']);
  assert.equal(templates[0].persona, '你是代码评审。');
  assert.equal(templates[1].persona, '');
  assert.notEqual(templates[0].avatarHue, templates[1].avatarHue);
});

test('installed experts take unused colors and a template can be created only once', () => {
  const reviewer = installedExpertToTemplate(installed, { persona: '你是代码评审。' });
  const writer = installedExpertToTemplate(
    {
      id: 'writer',
      runtimePackageName: 'research-writer',
      displayName: '研究写作',
      description: '整理论证。',
      installed: true,
      tags: [],
    },
    { persona: '你是研究写作。' },
  );
  const colored = assignInstalledExpertHues([reviewer, writer], [55, 110]);

  assert.deepEqual(colored.map((item) => item.avatarHue), [155, 190]);
  assert.equal(
    templateAlreadyCreated(
      { sourceTemplateId: 'english-coach', sourceTemplateName: '英语表达教练', category: 'education' },
      [{ name: '英语表达教练', category: 'education' }],
    ),
    true,
  );
  assert.equal(
    templateAlreadyCreated(
      { sourceTemplateId: 'code-reviewer', sourceTemplateName: '代码评审', category: 'custom' },
      [{ name: '代码评审', category: 'education' }],
    ),
    false,
  );
  assert.equal(
    templateAlreadyCreated(
      { sourceTemplateId: 'research-writer', sourceTemplateName: '研究写作', category: 'custom' },
      [{ name: '研究写作', category: 'custom' }],
    ),
    true,
  );
  assert.equal(
    templateAlreadyCreated(
      { sourceTemplateId: 'code-reviewer', sourceTemplateName: '代码评审', category: 'custom' },
      [{ name: '改过名字', category: 'custom', sourceTemplateId: 'code-reviewer' }],
    ),
    true,
  );
});
