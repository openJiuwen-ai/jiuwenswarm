import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveTeamMemberAvatar } from '../node_modules/.cache/team-member-avatar/teamMemberAvatar.js';

test('leader and user keep their dedicated avatars', () => {
  assert.equal(resolveTeamMemberAvatar('team_leader').kind, 'leader');
  assert.equal(resolveTeamMemberAvatar('user').kind, 'user');
});

test('an ordinary teammate falls back to the hashed illustration set', () => {
  const avatar = resolveTeamMemberAvatar('researcher-1', { role: 'teammate' });
  assert.equal(avatar.kind, 'member');
  assert.ok(avatar.backgroundColor);
});

test('a human member gets the human avatar set', () => {
  const byRole = resolveTeamMemberAvatar('reviewer-1', { role: 'human_agent' });
  // 老事件只带 mode='human'，两种表达等价
  const byMode = resolveTeamMemberAvatar('reviewer-1', { mode: 'human' });

  assert.equal(byRole.kind, 'human');
  assert.equal(byMode.kind, 'human');
  assert.equal(byRole.src, byMode.src);
  assert.notEqual(byRole.src, resolveTeamMemberAvatar('reviewer-1', { role: 'teammate' }).src);
});

test('claude and codex CLI members get distinct brand avatars', () => {
  const claude = resolveTeamMemberAvatar('cli-1', { role: 'teammate', cliAgent: 'claude' });
  const codex = resolveTeamMemberAvatar('cli-2', { role: 'teammate', cliAgent: 'codex' });

  assert.equal(claude.kind, 'cli');
  assert.equal(codex.kind, 'cli');
  assert.notEqual(claude.src, codex.src);
  // 品牌图标统一中性底，不走成员那套彩色底
  assert.equal(claude.backgroundColor, codex.backgroundColor);
});

test('CLI backend names are normalized before matching', () => {
  const claude = resolveTeamMemberAvatar('cli-1', { cliAgent: 'claude' });
  assert.equal(resolveTeamMemberAvatar('cli-1', { cliAgent: 'Claude-Code' }).src, claude.src);
  assert.equal(resolveTeamMemberAvatar('cli-1', { cliAgent: '  CLAUDE  ' }).src, claude.src);

  // 未知后端不冒充品牌头像，退回普通成员那套
  assert.equal(resolveTeamMemberAvatar('cli-9', { cliAgent: 'hermes' }).kind, 'member');
  assert.equal(resolveTeamMemberAvatar('cli-9', { cliAgent: '' }).kind, 'member');
});

test('CLI identity wins over human identity', () => {
  // 一个成员不可能既是真人又是 CLI，但真出现脏数据时优先认 CLI，避免拿人类头像
  // 去代表一个进程。
  const avatar = resolveTeamMemberAvatar('cli-1', { role: 'human_agent', cliAgent: 'codex' });
  assert.equal(avatar.kind, 'cli');
});
