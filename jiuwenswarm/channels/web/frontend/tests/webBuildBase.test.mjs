import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { resolveConfig } from 'vite';

async function loadModule(file, env) {
  const bundled = await build({
    entryPoints: [file], bundle: true, format: 'esm', write: false,
    define: { 'import.meta.env': JSON.stringify(env) }, logLevel: 'silent',
  });
  return import(`data:text/javascript;base64,${Buffer.from(bundled.outputFiles[0].text).toString('base64')}`);
}

for (const base of ['/', './', '/portal/user-web/', '/chat/']) {
  test(`chat and design routes round-trip under ${base}`, async () => {
    const routes = await loadModule('src/multi-session/routing/route.ts', { BASE_URL: base });
    for (const route of [
      { kind: 'chat-new' }, { kind: 'chat-session', sessionId: 'hello /世界' },
      { kind: 'design-project', projectId: 'project one' },
      { kind: 'design-project', projectId: 'project one', sessionId: 'session one' },
    ]) {
      const path = routes.chatRoutePath(route);
      assert.ok(path.startsWith('/'));
      const browserPath = new URL(path, 'https://example.test/chat/current').pathname;
      assert.deepEqual(routes.parseChatRoute(browserPath), route);
    }
    if (base === '/chat/') {
      assert.equal(routes.chatRoutePath({ kind: 'chat-new' }), '/chat/chat/new');
    }
    if (base === '/portal/user-web/') assert.equal(routes.parseChatRoute('/chat/new'), null);
  });
}

test('the actual Vite config defaults to root, accepts portal base and preserves Electron', async () => {
  const saved = { base: process.env.VITE_BASE_PATH, electron: process.env.ELECTRON };
  try {
    for (const [base, electron, expected] of [
      [undefined, undefined, '/'], ['', undefined, '/'], ['./', undefined, './'],
      ['/portal/user-web/', undefined, '/portal/user-web/'],
      ['/portal/user-web/', 'true', './'],
    ]) {
      if (base === undefined) delete process.env.VITE_BASE_PATH;
      else process.env.VITE_BASE_PATH = base;
      if (electron === undefined) delete process.env.ELECTRON;
      else process.env.ELECTRON = electron;
      const config = await resolveConfig({ configFile: 'vite.config.ts', logLevel: 'silent' }, 'build');
      assert.equal(config.base, expected);
    }
  } finally {
    if (saved.base === undefined) delete process.env.VITE_BASE_PATH;
    else process.env.VITE_BASE_PATH = saved.base;
    if (saved.electron === undefined) delete process.env.ELECTRON;
    else process.env.ELECTRON = saved.electron;
  }
});

test('API prefix stays separate from page base and preserves explicit URLs', async () => {
  const saved = globalThis.window;
  globalThis.window = { location: { protocol: 'https:', host: 'example.test' } };
  try {
    for (const edition of ['personal', 'enterprise']) {
      globalThis.window.__JIUWENSWARM_EDITION__ = edition;
      const env = await loadModule('src/utils/env.ts', { BASE_URL: '/portal/user-web/', VITE_API_PREFIX: '/portal/api' });
      assert.equal(env.getGatewayHttpBase(), `/portal/api/${edition === 'enterprise' ? 'gateway-api' : 'api'}/v1`);
      assert.equal(env.getWsBase(), 'wss://example.test/portal/api/ws');
      for (const value of ['https://obs.test/file?signature=abc', '//cdn.test/image.png', 'data:image/png;base64,x', 'blob:https://example.test/id']) {
        assert.equal(env.resolveApiUrl(value), value);
      }
      assert.equal(env.resolveApiUrl('/portal/api/file-api/download?token=abc'), '/portal/api/file-api/download?token=abc');
    }
    const explicit = await loadModule('src/utils/env.ts', {
      VITE_API_PREFIX: '/portal/api', VITE_GATEWAY_HTTP_BASE: 'https://gateway.test/api/v1', VITE_WS_BASE: 'wss://gateway.test/ws',
    });
    assert.equal(explicit.getGatewayHttpBase(), 'https://gateway.test/api/v1');
    assert.equal(explicit.getWsBase(), 'wss://gateway.test/ws');
  } finally {
    if (saved === undefined) delete globalThis.window;
    else globalThis.window = saved;
  }
});


test('URL utilities can be imported before a browser window exists', async () => {
  const env = await loadModule('src/utils/env.ts', {});
  assert.equal(env.getWebTransport(), 'websocket');
});

test('attachment and artifact URLs keep tenant scope when a portal prefix is configured', async () => {
  const saved = globalThis.window;
  globalThis.window = { location: { search: '?user_id=user1&group_id=group1&bot_id=bot1' } };
  try {
    for (const edition of ['personal', 'enterprise']) {
      globalThis.window.__JIUWENSWARM_EDITION__ = edition;
      const uploads = await loadModule('src/services/uploadFileUrl.ts', { VITE_API_PREFIX: '/portal/api' });
      const path = '/workspace/agent/sessions/session-one/uploads/image one.png';
      const url = new URL(uploads.uploadFileUrl(path), 'https://example.test');
      assert.equal(url.pathname, '/portal/api/file-api/raw-file');
      assert.equal(url.searchParams.get('path'), path);
      if (edition === 'enterprise') {
        assert.equal(url.searchParams.get('session_id'), 'session-one');
        for (const [key, value] of [['user_id', 'user1'], ['group_id', 'group1'], ['bot_id', 'bot1']]) {
          assert.equal(url.searchParams.get(key), value);
        }
        assert.equal(uploads.uploadFileUrl('/workspace/other/image.png'), undefined);
      }
    }
    const preview = await loadModule('src/components/ArtifactsPanel/filePreviewModel.ts', { VITE_API_PREFIX: '/portal/api' });
    assert.equal(preview.artifactDownloadUrl({ downloadUrl: '/file-api/download?token=a%2Fb' }), '/portal/api/file-api/download?token=a%2Fb');
    assert.equal(preview.artifactDownloadUrl({ downloadUrl: 'https://obs.test/file?signature=abc' }), 'https://obs.test/file?signature=abc');
    assert.equal(preview.artifactBinaryPreviewUrl({ downloadUrl: '/portal/api/file-api/download?token=a%2Fb' }, 'https://example.test'), '/portal/api/file-api/download?token=a%2Fb&inline=1');
  } finally {
    if (saved === undefined) delete globalThis.window;
    else globalThis.window = saved;
  }
});
