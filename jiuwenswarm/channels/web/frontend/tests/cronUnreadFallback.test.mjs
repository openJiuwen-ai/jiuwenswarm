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
try {
  ({ shouldMarkCronUnreadFromFinal } = await vite.ssrLoadModule(
    '/src/features/cron/cronUnreadFallback.ts',
  ));
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
