import test from 'node:test';
import assert from 'node:assert/strict';
import {
  isEnterpriseSourceSkillInstalled,
  resolveSourceInstalledLocalSkill,
} from '../node_modules/.cache/enterprise-skill-source-install/installMetadata.mjs';

const SOURCE_ID = 'swarmskillhub';
const marketItem = {
  source_id: SOURCE_ID,
  skill_id: 'skill-a-v2',
  name: 'shared-skill',
  display_name: '共享技能',
};

test('同名不同源：user/本地/广场自装同名不触发名称回退，保持未安装可装', () => {
  const localSkills = [
    {
      name: 'shared-skill',
      display_name: '共享技能',
      origin: 'clawhub:someone/shared-skill',
      source_type: 'user',
      skill_type: 'skill',
    },
    {
      name: 'shared-skill',
      display_name: '共享技能',
      origin: 'local:shared-skill',
      source_type: 'user',
      skill_type: 'skill',
    },
  ];

  assert.equal(resolveSourceInstalledLocalSkill(marketItem, SOURCE_ID, localSkills), null);
  assert.equal(
    isEnterpriseSourceSkillInstalled(marketItem, SOURCE_ID, localSkills, new Set(), true, false),
    false,
  );
});

test('内置同名：无企业源 origin 的 builtin 靠名称回退视为已装（去试试）', () => {
  const localSkills = [
    {
      name: 'shared-skill',
      display_name: '蜂群技能自动生成专家',
      origin: null,
      source_type: 'builtin',
      skill_type: 'swarm_skill',
    },
  ];

  const resolved = resolveSourceInstalledLocalSkill(marketItem, SOURCE_ID, localSkills);
  assert.ok(resolved);
  assert.equal(resolved.name, 'shared-skill');
  assert.equal(resolved.source_type, 'builtin');
  assert.equal(
    isEnterpriseSourceSkillInstalled(marketItem, SOURCE_ID, localSkills, new Set(), true, false),
    true,
  );
});

test('企业源 origin 精确匹配：同名无关，按 source_id:skill_id 判定已装', () => {
  const identity = `${SOURCE_ID}:skill-a-v2`;
  const localSkills = [
    {
      name: 'other-local-name',
      display_name: '本地包名可与广场不同',
      origin: identity,
      source_type: 'user',
      skill_type: 'skill',
    },
  ];

  const resolved = resolveSourceInstalledLocalSkill(marketItem, SOURCE_ID, localSkills);
  assert.ok(resolved);
  assert.equal(resolved.name, 'other-local-name');
  assert.equal(
    isEnterpriseSourceSkillInstalled(
      marketItem,
      SOURCE_ID,
      localSkills,
      new Set([identity]),
      true,
      false,
    ),
    true,
  );
});

test('prebuilt 同名也可名称回退；仅 display_name 撞上 user 技能不回退', () => {
  const prebuiltHit = resolveSourceInstalledLocalSkill(marketItem, SOURCE_ID, [
    {
      name: 'shared-skill',
      display_name: '预置共享',
      source_type: 'prebuilt',
    },
  ]);
  assert.ok(prebuiltHit);
  assert.equal(prebuiltHit.source_type, 'prebuilt');

  const displayOnlyUser = resolveSourceInstalledLocalSkill(marketItem, SOURCE_ID, [
    {
      name: 'totally-different',
      display_name: '共享技能',
      origin: 'teamskillshub:other',
      source_type: 'user',
    },
  ]);
  assert.equal(displayOnlyUser, null);
});
