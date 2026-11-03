import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { webcrypto } from 'node:crypto';

await build({
  stdin: {
    contents:
      "export { persistEnterpriseAttachments } from './src/services/obsUpload'; export { setRuntimeScope } from './src/services/runtimeScope'; export { fileDownloadHead, prepareFileDownload } from './src/services/fileDownload'; export { uploadFileUrl } from './src/services/uploadFileUrl';",
    resolveDir: process.cwd(),
  },
  bundle: true,
  format: 'esm',
  platform: 'node',
  define: { 'import.meta.env': '{}' },
  outfile: 'node_modules/.cache/enterprise/files.mjs',
});
const { persistEnterpriseAttachments, setRuntimeScope, fileDownloadHead, prepareFileDownload, uploadFileUrl } =
  await import('../node_modules/.cache/enterprise/files.mjs');
const item = { filename: '中文.txt', base64Data: 'aGVsbG8=' };
function setup() {
  const values = new Map([['openjiuwen_access_token', 'token']]);
  globalThis.localStorage = {
    getItem: (key) => values.get(key) ?? null,
    setItem: (key, value) => values.set(key, value),
    removeItem: (key) => values.delete(key),
  };
  globalThis.sessionStorage = { removeItem() {} };
  globalThis.document = { cookie: '' };
  globalThis.window = {
    location: {
      search: '',
      protocol: 'http:',
      href: 'http://localhost/chat/',
      origin: 'http://localhost',
      pathname: '/chat/',
      replace() {},
    },
    localStorage,
    __JIUWENSWARM_EDITION__: 'enterprise',
  };
  setRuntimeScope({ userId: 'u', groupId: 'g', botId: 'b' });
  globalThis.fetch = async () => new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: '中文.txt' }));
}

test('enterprise original image URL carries existing tenant and upload session, not credentials', () => {
  setup();
  const path = '/tenant/agent/sessions/s/uploads/中文 图片.png';
  const result = new URL(uploadFileUrl(path), 'http://localhost');
  assert.equal(result.pathname, '/file-api/raw-file');
  assert.equal(result.searchParams.get('path'), path);
  assert.equal(result.searchParams.get('session_id'), 's');
  assert.equal(result.searchParams.get('user_id'), 'u');
  assert.equal(result.searchParams.get('group_id'), 'g');
  assert.equal(result.searchParams.get('bot_id'), 'b');
  assert.equal(result.searchParams.has('token'), false);
  assert.equal(result.searchParams.has('access_token'), false);
});

test('enterprise original image URL follows context switches and excludes non-upload paths', () => {
  setup();
  const path = '/tenant/agent/sessions/s/uploads/photo.png';
  setRuntimeScope({ userId: 'other', groupId: 'other-g', botId: 'other-b' });
  const result = new URL(uploadFileUrl(path), 'http://localhost');
  assert.equal(result.searchParams.get('user_id'), 'other');
  assert.equal(result.searchParams.get('group_id'), 'other-g');
  assert.equal(result.searchParams.get('bot_id'), 'other-b');
  for (const outside of ['/etc/passwd', '/tenant/agent/jiuwenclaw_workspace/photo.png',
    '/tenant/agent/sessions/../uploads/photo.png']) assert.equal(uploadFileUrl(outside), undefined);
});

test('personal original image URL remains byte-for-byte unchanged', () => {
  setup();
  window.__JIUWENSWARM_EDITION__ = 'personal';
  const path = '/local/project/中文 图片.png';
  assert.equal(uploadFileUrl(path), `/file-api/raw-file?path=${encodeURIComponent(path)}`);
});

for (const kind of ['image', 'document']) {
  test(`${kind} upload and cleanup work over non-secure HTTP without randomUUID`, async () => {
    setup();
    const descriptor = Object.getOwnPropertyDescriptor(globalThis, 'crypto');
    Object.defineProperty(globalThis, 'crypto', {
      configurable: true,
      value: { getRandomValues: webcrypto.getRandomValues.bind(webcrypto) },
    });
    const requests = [];
    globalThis.fetch = async (url, init) => {
      requests.push({ method: init.method, headers: new Headers(init.headers) });
      return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: item.filename }));
    };
    try {
      await persistEnterpriseAttachments(
        async (method) => method === 'file.import_url'
          ? { files: [{ path: '/tenant/a', filename: item.filename }] }
          : {},
        '', 's', [item], kind,
      );
      assert.deepEqual(requests.map((request) => request.method), ['POST', 'DELETE']);
      const ids = requests.map((request) => request.headers.get('X-Request-Id'));
      for (const id of ids) assert.match(id, /^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$/);
      assert.notEqual(ids[0], ids[1]);
      for (const request of requests) assert.equal(request.headers.get('X-User-Id'), 'u');
    } finally {
      if (descriptor) Object.defineProperty(globalThis, 'crypto', descriptor);
      else delete globalThis.crypto;
    }
  });
}

test('signed URL stays in import; persist/chat receive only AS paths', async () => {
  setup();
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    if (method === 'file.import_url') return { files: [{ path: '/tenant/uploads/file.txt', filename: 'file.txt' }] };
    return { media_items: params.documents };
  };
  const result = await persistEnterpriseAttachments(request, 'hello', 's', [item], 'document');
  assert.equal(calls[0][1].files[0].url, 'http://obs/signed');
  assert.ok(!JSON.stringify(calls[1]).includes('http://obs'));
  assert.equal(result.media_items[0].path, '/tenant/uploads/file.txt');
});

for (const failure of ['import', 'persist', 'none']) {
  test(`temporary OBS object cleanup after ${failure === 'none' ? 'success' : failure + ' failure'}`, async () => {
    setup();
    const deleted = [];
    globalThis.fetch = async (url, init) => {
      if (init.method === 'DELETE') {
        deleted.push({ url: JSON.parse(init.body).url, user: new Headers(init.headers).get('X-User-Id') });
        return new Response(JSON.stringify({ ok: true }));
      }
      return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: 'a' }));
    };
    const methods = [];
    const operation = persistEnterpriseAttachments(
      async (method) => {
        methods.push(method);
        if (method === 'file.import_url') {
          if (failure === 'import') throw new Error('import failed');
          return { files: [{ path: '/tenant/a', filename: 'a' }] };
        }
        if (method === 'document.persist' && failure === 'persist') throw new Error('persist failed');
        return {};
      },
      '',
      's',
      [item],
      'document',
    );
    if (failure === 'none') await operation;
    else await assert.rejects(operation, new RegExp(failure + ' failed'));
    assert.deepEqual(deleted, [{ url: 'http://obs/signed', user: 'u' }]);
    assert.equal(methods.includes('media.discard'), failure === 'persist');
  });
}

test('cleanup keeps captured scope and does not replace the original import error', async () => {
  setup();
  const cleanupScopes = [];
  globalThis.fetch = async (url, init) => {
    if (init.method === 'DELETE') {
      cleanupScopes.push(new Headers(init.headers).get('X-User-Id'));
      throw new Error('cleanup unavailable');
    }
    return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: 'a' }));
  };
  const previousWarn = console.warn;
  const warnings = [];
  console.warn = (...args) => warnings.push(args);
  try {
    await assert.rejects(
      persistEnterpriseAttachments(
        async (method) => {
          assert.equal(method, 'file.import_url');
          setRuntimeScope({ userId: 'other', groupId: 'g', botId: 'b' });
          throw new Error('original import failure');
        },
        '',
        's',
        [item],
        'document',
      ),
      /original import failure/,
    );
  } finally {
    console.warn = previousWarn;
  }
  assert.deepEqual(cleanupScopes, ['u']);
  assert.equal(warnings.length, 1);
});

for (const status of [200, 401, 403, 404, 410, 405, 500, 502, 503]) {
  test(`download HEAD ${status} ${status === 200 || status === 405 || status >= 500 ? 'permits' : 'blocks'} GET`, async () => {
    setup();
    globalThis.fetch = async () => new Response(null, { status });
    assert.equal(
      await prepareFileDownload('/file-api/download?token=signed'),
      status === 200 || status === 405 || status >= 500,
    );
  });
}

test('HEAD network failure does not make native download depend on Identity credentials', async () => {
  setup();
  globalThis.fetch = async () => {
    throw new TypeError('network unavailable');
  };
  assert.equal(await prepareFileDownload('/file-api/download?token=signed'), true);
  localStorage.removeItem('openjiuwen_access_token');
  assert.equal(await prepareFileDownload('/file-api/download?token=signed'), true);
});

test('download HEAD does not call Identity even on 401', async () => {
  setup();
  const urls = [];
  globalThis.fetch = async (url) => {
    urls.push(url);
    return new Response(null, { status: 401 });
  };
  assert.equal(await prepareFileDownload('/file-api/download?token=signed'), false);
  assert.deepEqual(urls, ['/file-api/download?token=signed']);
});

test('persist failure cleans imported paths without sending chat', async () => {
  setup();
  const calls = [];
  const request = async (method, params) => {
    calls.push(method);
    if (method === 'file.import_url') return { files: [{ path: '/tenant/a', filename: 'a' }] };
    if (method === 'document.persist') throw new Error('parse failed');
  };
  await assert.rejects(persistEnterpriseAttachments(request, '', 's', [item], 'document'), /parse failed/);
  assert.deepEqual(calls, ['file.import_url', 'document.persist', 'media.discard']);
});

test('context switch aborts before import and never cleans a different tenant', async () => {
  setup();
  globalThis.fetch = async () => {
    setRuntimeScope({ userId: 'other', groupId: 'g', botId: 'b' });
    return new Response(JSON.stringify({ ok: true, url: 'http://obs/signed', name: 'a' }));
  };
  let calls = 0;
  await assert.rejects(
    persistEnterpriseAttachments(
      async () => {
        calls++;
      },
      '',
      's',
      [item],
      'document',
    ),
    /context changed/,
  );
  assert.equal(calls, 0);
});

test('download HEAD never injects platform credentials into native links', async () => {
  setup();
  window.__JIUWENSWARM_EDITION__ = 'enterprise';
  Object.assign(window.location, { href: 'http://localhost/chat/', origin: 'http://localhost' });
  const authorizations = [];
  globalThis.fetch = async (url, init) => {
    authorizations.push(new Headers(init.headers).get('Authorization'));
    return new Response('', { status: 200 });
  };
  await fileDownloadHead('/file-api/download?token=signed');
  await fileDownloadHead('http://untrusted.example/file-api/download');
  assert.deepEqual(authorizations, [null, null]);
});
