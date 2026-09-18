import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const zh = JSON.parse(readFileSync(new URL('../src/i18n/locales/zh.json', import.meta.url), 'utf8'));
const en = JSON.parse(readFileSync(new URL('../src/i18n/locales/en.json', import.meta.url), 'utf8'));

test('团队空状态文案使用「暂无成员」而非误导性的「请选择」', () => {
  assert.equal(zh.team.noMembers, '暂无成员');
  assert.equal(en.team.noMembers, 'No members yet');
  // 误导性的「请选择 Team 成员」文案应被移除
  assert.equal(zh.team.selectMember, undefined);
  assert.equal(en.team.selectMember, undefined);
});

test('左侧成员列表空状态文案保持不变（暂无成员数据）', () => {
  assert.equal(zh.team.noMemberData, '暂无成员数据');
  assert.equal(en.team.noMemberData, 'No member data');
});
