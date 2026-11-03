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
let shouldMarkCronUnreadFromFinal;
let applyCronSettleUnread;
let resetCronSettleAcknowledgmentsForTests;
try {
  ({
    shouldMarkCronUnreadFromFinal,
    applyCronSettleUnread,
    resetCronSettleAcknowledgmentsForTests,
  } = await vite.ssrLoadModule('/src/features/cron/cronUnreadFallback.ts'));
} finally {
  await vite.close();
}

test('chat.final without terminal status does not mark unread', () => {
  assert.equal(shouldMarkCronUnreadFromFinal({ is_placeholder: false }), false);
  assert.equal(shouldMarkCronUnreadFromFinal({ is_placeholder: false, status: '' }), false);
  assert.equal(shouldMarkCronUnreadFromFinal({ is_placeholder: false, status: 'running' }), false);
  assert.equal(
    shouldMarkCronUnreadFromFinal({ is_placeholder: false, status: 'awaiting_approval' }),
    false,
  );
});

test('chat.final with terminal status marks unread', () => {
  assert.equal(shouldMarkCronUnreadFromFinal({ is_placeholder: false, status: 'succeeded' }), true);
  assert.equal(shouldMarkCronUnreadFromFinal({ is_placeholder: false, status: 'failed' }), true);
  assert.equal(shouldMarkCronUnreadFromFinal({ is_placeholder: true, status: 'succeeded' }), false);
});

test('applyCronSettleUnread: viewing clears once; later leave replay does not re-mark', () => {
  resetCronSettleAcknowledgmentsForTests();
  const marked = [];
  const cleared = [];
  const mark = (id) => marked.push(id);
  const clear = (id) => cleared.push(id);

  applyCronSettleUnread({
    jobId: 'job-1', sessionId: 'cron_abc', viewing: true, mark, clear,
  });
  assert.deepEqual(cleared, ['job-1']);
  assert.deepEqual(marked, []);

  // 模拟切走后 live 回放又来 processing=false
  applyCronSettleUnread({
    jobId: 'job-1', sessionId: 'cron_abc', viewing: false, mark, clear,
  });
  assert.deepEqual(marked, []);
  assert.deepEqual(cleared, ['job-1']);
});

test('applyCronSettleUnread: background first settle marks unread', () => {
  resetCronSettleAcknowledgmentsForTests();
  const marked = [];
  const cleared = [];
  applyCronSettleUnread({
    jobId: 'job-2',
    sessionId: 'cron_bg',
    viewing: false,
    mark: (id) => marked.push(id),
    clear: (id) => cleared.push(id),
  });
  assert.deepEqual(marked, ['job-2']);
  assert.deepEqual(cleared, []);
});
