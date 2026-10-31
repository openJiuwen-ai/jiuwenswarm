import assert from 'node:assert/strict';
import test from 'node:test';
import { createServer } from 'vite';

const vite = await createServer({
  configFile: false,
  cacheDir: 'node_modules/.cache/cron-active-session/vite',
  server: { middlewareMode: true, hmr: false, watch: null },
  define: {
    'import.meta.env.DEV': false,
    'import.meta.env.VITE_JIUWENSWARM_EDITION': JSON.stringify('personal'),
  },
});
let pickNewActiveCronSessions;
try {
  ({ pickNewActiveCronSessions } = await vite.ssrLoadModule(
    '/src/features/cron/cronActiveSessionPick.ts',
  ));
} finally {
  await vite.close();
}

test('pickNewActiveCronSessions returns unseen active sessions (any id shape)', () => {
  const jobs = [
    { id: 'job-1', active_session_id: 'cron_abc_job-1' },
    { id: 'job-2', active_session_id: '' },
    { id: 'job-3', active_session_id: 'web_unified_sess' },
    { id: 'job-4', active_session_id: 'cron_dup' },
  ];
  const first = pickNewActiveCronSessions(jobs, { 'job-4': 'cron_dup' });
  assert.deepEqual(first, [
    { jobId: 'job-1', sessionId: 'cron_abc_job-1' },
    { jobId: 'job-3', sessionId: 'web_unified_sess' },
  ]);
});

test('pickNewActiveCronSessions empty when already adopted', () => {
  const jobs = [{ id: 'job-1', active_session_id: 'cron_abc' }];
  assert.deepEqual(pickNewActiveCronSessions(jobs, { 'job-1': 'cron_abc' }), []);
});
