import assert from 'node:assert/strict';
import test from 'node:test';

import {
  getProjectMenuItems,
  getSessionMenuItems,
} from '../node_modules/.cache/sidebar-model/multi-session/sidebar/sidebarMenuSchema.js';
import { getViewedSessionId } from '../node_modules/.cache/sidebar-model/multi-session/sidebar/sidebarModel.js';
import {
  formatSessionTime,
  getSessionActivityAt,
  sortSessionsForSidebar,
} from '../node_modules/.cache/sidebar-model/multi-session/sidebar/sidebarModel.js';

const t = (key) => key;
const findItem = (items, key) => items.find((item) => item.key === key);

test('ordinary project menu shows pin, rename, delete and archive-sessions', () => {
  const items = getProjectMenuItems(false, t);
  assert.deepEqual(items.map((item) => item.key), ['pin', 'rename', 'delete', 'archive-sessions']);
  assert.equal(findItem(items, 'delete').danger, true);
  assert.equal(findItem(items, 'archive-sessions').disabled, undefined);
});

test('default project menu only shows archive-sessions', () => {
  const items = getProjectMenuItems(false, t, { isDefault: true });
  assert.deepEqual(items.map((item) => item.key), ['archive-sessions']);
});

test('archive-sessions is disabled when archiveSessionsDisabled is true', () => {
  const items = getProjectMenuItems(false, t, { archiveSessionsDisabled: true });
  assert.equal(findItem(items, 'archive-sessions').disabled, true);

  const defaultItems = getProjectMenuItems(false, t, { isDefault: true, archiveSessionsDisabled: true });
  assert.equal(findItem(defaultItems, 'archive-sessions').disabled, true);
});

test('archive-sessions stays enabled when archiveSessionsDisabled is false or omitted', () => {
  const explicit = getProjectMenuItems(false, t, { archiveSessionsDisabled: false });
  assert.notEqual(findItem(explicit, 'archive-sessions').disabled, true);

  const omitted = getProjectMenuItems(false, t);
  assert.notEqual(findItem(omitted, 'archive-sessions').disabled, true);
});

test('pinned state switches the pin label without affecting other items', () => {
  const pinned = getProjectMenuItems(true, t);
  assert.equal(findItem(pinned, 'pin').label, 'multiSession.project.unpinProject');
  assert.deepEqual(pinned.map((item) => item.key), ['pin', 'rename', 'delete', 'archive-sessions']);

  const unpinned = getProjectMenuItems(false, t);
  assert.equal(findItem(unpinned, 'pin').label, 'multiSession.project.pinProject');
  assert.deepEqual(unpinned.map((item) => item.key), ['pin', 'rename', 'delete', 'archive-sessions']);
});

test('cron session menu omits archive but keeps delete', () => {
  const items = getSessionMenuItems(false, t, { archivable: false });
  assert.deepEqual(items.map((item) => item.key), ['pin', 'rename', 'delete']);
});

test('project-scoped session uses conversation pin labels', () => {
  const items = getSessionMenuItems(true, t, { scope: 'project' });
  assert.equal(findItem(items, 'pin').label, 'multiSession.project.unpinConversation');
});

test('the open design canvas decides the viewed session, not the chat session', () => {
  assert.equal(getViewedSessionId(true, 'chat-1', 'design-1'), 'design-1');
  assert.equal(getViewedSessionId(true, 'chat-1', undefined), null);
  assert.equal(getViewedSessionId(false, 'chat-1', undefined), 'chat-1');
  assert.equal(getViewedSessionId(false, null, undefined), null);
});

// ── 侧栏会话时间 ────────────────────────────────────────────────
// 回归：时间字段缺失/为 0 时曾被 `?? Date.now()` 兜底成"刚刚"，并因此顶到列表最前。

const NOW = new Date('2026-11-10T12:00:00+08:00').getTime();

test('missing or zero activity time is not rendered as "just now"', () => {
  assert.equal(getSessionActivityAt({}), 0);
  assert.equal(getSessionActivityAt({ last_user_message_at: 0, last_message_at: 0 }), 0);
  assert.equal(formatSessionTime(getSessionActivityAt({}), NOW, 'zh', t), '');
  assert.equal(formatSessionTime(Number.NaN, NOW, 'zh', t), '');
});

test('invalid date strings fall through to the next candidate field', () => {
  const session = {
    last_user_message_at: 'not-a-date',
    last_message_at: NOW - 3 * 86_400_000,
  };
  assert.equal(getSessionActivityAt(session), NOW - 3 * 86_400_000);
});

test('sessions without usable time sort after sessions with time', () => {
  const sessions = [
    { session_id: 'unknown' },
    { session_id: 'old', last_message_at: NOW - 30 * 86_400_000 },
    { session_id: 'recent', last_message_at: NOW - 60_000 },
  ];
  assert.deepEqual(sortSessionsForSidebar(sessions).map((s) => s.session_id), ['recent', 'old', 'unknown']);
});

test('relative time is used within seven days', () => {
  assert.equal(formatSessionTime(NOW - 30_000, NOW, 'zh', t), 'time.justNow');
  assert.equal(formatSessionTime(NOW - 5 * 60_000, NOW, 'zh', t), 'time.minutesAgo');
  assert.equal(formatSessionTime(NOW - 5 * 3_600_000, NOW, 'zh', t), 'time.hoursAgo');
  assert.equal(formatSessionTime(NOW - 5 * 86_400_000, NOW, 'zh', t), 'time.daysAgo');
});

test('older sessions show the year when it differs from the current year', () => {
  const lastYear = new Date('2025-01-05T09:00:00+08:00').getTime();
  const sameYear = new Date('2026-01-05T09:00:00+08:00').getTime();
  assert.match(formatSessionTime(lastYear, NOW, 'zh', t), /2025/);
  assert.doesNotMatch(formatSessionTime(sameYear, NOW, 'zh', t), /2026/);
});
