import assert from 'node:assert/strict';
import test from 'node:test';

import { filterSkillSuggestions } from '../node_modules/.cache/skill-argument-completion/skillArgumentCompletion.mjs';

const skills = [
  { name: 'doc-checker', skill_type: 'skill', description: '检查文档' },
  { name: 'weekly-pack', skill_type: 'skillpack', description: '周报技能包' },
  { name: 'matplotlib-simple-plot', skill_type: 'multimodal_skill', description: '绘制图表' },
  { name: 'team-research', skill_type: 'swarm_skill', description: '团队研究' },
  { name: 'legacy-skill', description: '无类型' },
];

test('single agent search includes multimodal skills', () => {
  const matched = filterSkillSuggestions(skills, 'matplotlib', false).map((skill) => skill.name);
  assert.deepEqual(matched, ['matplotlib-simple-plot']);
});

test('single agent list keeps ordinary skills, packs, and untyped skills', () => {
  const names = filterSkillSuggestions(skills, '', false).map((skill) => skill.name);
  assert.deepEqual(names, ['doc-checker', 'weekly-pack', 'matplotlib-simple-plot', 'legacy-skill']);
});

test('team mode stays limited to swarm skills', () => {
  const names = filterSkillSuggestions(skills, 'matplot', true).map((skill) => skill.name);
  assert.deepEqual(names, []);
  assert.deepEqual(
    filterSkillSuggestions(skills, '', true).map((skill) => skill.name),
    ['team-research'],
  );
});

test('evolution command completion includes multimodal skills and skips packs', () => {
  const names = filterSkillSuggestions(skills, '', false, true).map((skill) => skill.name);
  assert.deepEqual(names, ['doc-checker', 'matplotlib-simple-plot', 'legacy-skill']);
});
