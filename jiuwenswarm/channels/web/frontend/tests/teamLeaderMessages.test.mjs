import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findActiveTeamLeaderMessage,
  findTeamLeaderMessageWithFile,
} from '../node_modules/.cache/team-leader-messages/teamLeaderMessages.mjs';

/** 构造一条 team-leader 消息（默认带 teamStream 与 fileItems）。 */
function leaderMessage(overrides = {}) {
  return {
    id: 'team-leader-1',
    role: 'system',
    content: '',
    timestamp: '2026-11-26T00:00:00.000Z',
    teamStream: { requestId: 'req-1' },
    fileItems: [],
    ...overrides,
  };
}

function userMessage(overrides = {}) {
  return {
    id: 'user-1',
    role: 'user',
    content: '生成 qa-red.txt',
    timestamp: '2026-11-26T00:00:01.000Z',
    ...overrides,
  };
}

test('findTeamLeaderMessageWithFile matches by resolved path identity', () => {
  const existing = leaderMessage({
    id: 'team-leader-a',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/qa-red.txt' }],
  });
  // 同一文件二次到达，path 相同 → 命中已存在的 leader 消息
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  assert.equal(findTeamLeaderMessageWithFile([existing], incoming), existing);
});

test('findTeamLeaderMessageWithFile falls back to name+size when path absent', () => {
  const existing = leaderMessage({
    id: 'team-leader-a',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '' }],
  });
  const incoming = [{ name: 'qa-red.txt', size: 7 }];
  assert.equal(findTeamLeaderMessageWithFile([existing], incoming), existing);
});

test('findTeamLeaderMessageWithFile returns undefined when no overlap', () => {
  const existing = leaderMessage({
    id: 'team-leader-a',
    fileItems: [{ name: 'other.txt', size: 100, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/other.txt' }],
  });
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  assert.equal(findTeamLeaderMessageWithFile([existing], incoming), undefined);
});

test('findTeamLeaderMessageWithFile ignores team-leader messages without fileItems', () => {
  const noFiles = leaderMessage({ id: 'team-leader-a', fileItems: undefined });
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  assert.equal(findTeamLeaderMessageWithFile([noFiles], incoming), undefined);
});

test('findTeamLeaderMessageWithFile only searches the current user turn', () => {
  // 上一轮的 leader 持有同文件，但中间插了一条新的 user 消息 → 不应被命中
  const previousTurnLeader = leaderMessage({
    id: 'team-leader-old',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/qa-red.txt' }],
  });
  const messages = [
    previousTurnLeader,
    userMessage({ id: 'user-2', content: '新一轮' }),
  ];
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  assert.equal(findTeamLeaderMessageWithFile(messages, incoming), undefined);
});

test('findTeamLeaderMessageWithFile returns the latest matching team-leader message', () => {
  const older = leaderMessage({
    id: 'team-leader-1',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/qa-red.txt' }],
  });
  const newer = leaderMessage({
    id: 'team-leader-2',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/qa-red.txt' }],
  });
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  // 自后向前查找，应命中较新的 team-leader-2
  assert.equal(findTeamLeaderMessageWithFile([older, newer], incoming), newer);
});

test('findTeamLeaderMessageWithFile ignores non-team-leader messages with overlapping files', () => {
  const assistantMsg = {
    id: 'assistant-1',
    role: 'assistant',
    content: '',
    timestamp: '2026-11-26T00:00:00.000Z',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/qa-red.txt' }],
  };
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  assert.equal(findTeamLeaderMessageWithFile([assistantMsg], incoming), undefined);
});

test('findTeamLeaderMessageWithFile returns undefined for empty incoming', () => {
  const existing = leaderMessage({
    id: 'team-leader-a',
    fileItems: [{ name: 'qa-red.txt', size: 7, mime_type: 'text/plain', download_url: '', download_token: '', path: '/ws/qa-red.txt' }],
  });
  assert.equal(findTeamLeaderMessageWithFile([existing], []), undefined);
});

// 回归：findActiveTeamLeaderMessage 仍要求 teamStream.requestId 匹配
test('findActiveTeamLeaderMessage requires teamStream requestId match', () => {
  const active = leaderMessage({ id: 'team-leader-a', teamStream: { requestId: 'req-1' } });
  assert.equal(findActiveTeamLeaderMessage([active], 'req-1'), active);
  assert.equal(findActiveTeamLeaderMessage([active], 'req-other'), undefined);
});

// 回归：chat.final 清掉 teamStream 后 findActiveTeamLeaderMessage 命中不到
// （这正是 findTeamLeaderMessageWithFile 作为兜底的场景）
test('findActiveTeamLeaderMessage misses after teamStream cleared by chat.final', () => {
  const finalized = leaderMessage({ id: 'team-leader-a', teamStream: undefined, fileItems: [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }] });
  assert.equal(findActiveTeamLeaderMessage([finalized], 'req-1'), undefined);
  // 兜底 helper 仍能按文件身份命中
  const incoming = [{ name: 'qa-red.txt', size: 7, path: '/ws/qa-red.txt' }];
  assert.equal(findTeamLeaderMessageWithFile([finalized], incoming), finalized);
});
