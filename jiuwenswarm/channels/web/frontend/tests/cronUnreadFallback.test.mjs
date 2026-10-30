import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/cron-unread-fallback/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: {
    'import.meta.env.DEV': false,
    'import.meta.env.VITE_JIUWENSWARM_EDITION': JSON.stringify('personal'),
  },
});
let reconcileCronUnreadFromJobs;
try {
  ({ reconcileCronUnreadFromJobs } = await vite.ssrLoadModule(
    '/src/features/cron/cronUnreadFallback.ts',
  ));
} finally {
  await vite.close();
}

test('first sight of last_session_id only seeds watermark, does not mark unread', () => {
  const { nextSeen, unreadJobIds } = reconcileCronUnreadFromJobs(
    [{ id: 'job-1', last_session_id: 'cron_abc' }],
    {},
  );
  assert.deepEqual(nextSeen, { 'job-1': 'cron_abc' });
  assert.deepEqual(unreadJobIds, []);
});

test('changed last_session_id marks unread and keeps old watermark until clear', () => {
  const { nextSeen, unreadJobIds } = reconcileCronUnreadFromJobs(
    [{ id: 'job-1', last_session_id: 'cron_new' }],
    { 'job-1': 'cron_old' },
  );
  assert.deepEqual(unreadJobIds, ['job-1']);
  // 未读未清前水位仍是旧值，重复 list 不会丢未读信号
  assert.equal(nextSeen['job-1'], 'cron_old');
});

test('same last_session_id does not mark unread', () => {
  const { nextSeen, unreadJobIds } = reconcileCronUnreadFromJobs(
    [{ id: 'job-1', last_session_id: 'cron_same' }],
    { 'job-1': 'cron_same' },
  );
  assert.deepEqual(unreadJobIds, []);
  assert.equal(nextSeen['job-1'], 'cron_same');
});

test('empty last_session_id is ignored', () => {
  const { nextSeen, unreadJobIds } = reconcileCronUnreadFromJobs(
    [{ id: 'job-1', last_session_id: '' }, { id: 'job-2' }],
    { 'job-1': 'cron_old' },
  );
  assert.deepEqual(unreadJobIds, []);
  assert.equal(nextSeen['job-1'], 'cron_old');
  assert.equal(nextSeen['job-2'], undefined);
});
