import test from 'node:test';
import assert from 'node:assert/strict';

const {
  SKILL_ZIP_MAX_BYTES,
  setRuntimeScope,
  stageSkillZip,
} = await import('../node_modules/.cache/enterprise-skill-upload/exports.mjs');

const OBJECT_URL = 'https://obs.example.com/skills/demo.zip';
const STAGED_PATH = '/data/hxy/profile/agent/sessions/s-1/uploads/demo.zip';

function makeZip(text = 'hello-skill') {
  return new File([new TextEncoder().encode(text)], 'demo.zip', { type: 'application/zip' });
}

function uploadOk(url = OBJECT_URL) {
  return new Response(JSON.stringify({ ok: true, url, name: 'demo.zip' }), {
    status: 200,
    headers: { 'content-type': 'application/json' },
  });
}

/** Replace global fetch and record every relay call made by stageSkillZip. */
function stubFetch(handler) {
  const calls = [];
  globalThis.fetch = async (input, init = {}) => {
    const url = typeof input === 'string' ? input : input.url;
    const call = {
      url,
      method: init.method ?? 'GET',
      headers: new Headers(init.headers),
      body: init.body,
    };
    calls.push(call);
    return handler(call, calls);
  };
  return calls;
}

function stubRequest(result) {
  const calls = [];
  const request = async (method, params, options) => {
    calls.push({ method, params, options });
    if (result instanceof Error) throw result;
    return result;
  };
  return { request, calls };
}

test.beforeEach(() => {
  setRuntimeScope({});
});

test('拒绝非 .zip 包，且不发起任何网络请求', async () => {
  const fetchCalls = stubFetch(() => uploadOk());
  const { request, calls } = stubRequest({ files: [{ path: STAGED_PATH }] });

  await assert.rejects(
    stageSkillZip(request, { name: 'demo.skill', size: 10 }, 's-1'),
    /Only \.zip skill packages are supported/,
  );
  assert.equal(fetchCalls.length, 0);
  assert.equal(calls.length, 0);
});

test('拒绝超过 50 MiB 的包，且不发起任何网络请求', async () => {
  const fetchCalls = stubFetch(() => uploadOk());
  const { request, calls } = stubRequest({ files: [{ path: STAGED_PATH }] });

  await assert.rejects(
    stageSkillZip(request, { name: 'demo.zip', size: SKILL_ZIP_MAX_BYTES + 1 }, 's-1'),
    /Skill package exceeds 50 MiB/,
  );
  assert.equal(fetchCalls.length, 0);
  assert.equal(calls.length, 0);
});

test('OBS 中继失败时抛 HTTP 状态，不调用安装', async () => {
  stubFetch(() => new Response('nope', { status: 502 }));
  const { request, calls } = stubRequest({ files: [{ path: STAGED_PATH }] });

  await assert.rejects(stageSkillZip(request, makeZip(), 's-1'), /Upload failed \(HTTP 502\)/);
  assert.equal(calls.length, 0);
});

test('OBS 中继返回业务错误时透出错误信息，不触发临时对象清理', async () => {
  const fetchCalls = stubFetch(
    () =>
      new Response(JSON.stringify({ ok: false, error: 'obs rejected' }), {
        status: 200,
        headers: { 'content-type': 'application/json' },
      }),
  );
  const { request, calls } = stubRequest({ files: [{ path: STAGED_PATH }] });

  await assert.rejects(stageSkillZip(request, makeZip(), 's-1'), /obs rejected/);
  assert.equal(calls.length, 0);
  assert.equal(fetchCalls.filter((call) => call.method === 'DELETE').length, 0);
});

test('成功链路：base64 上送、按会话落到 uploads、cleanup 删除 OBS 临时对象', async () => {
  const fetchCalls = stubFetch((call) =>
    call.method === 'DELETE' ? new Response('{}', { status: 200 }) : uploadOk(),
  );
  const { request, calls } = stubRequest({ files: [{ path: STAGED_PATH }] });
  setRuntimeScope({ agentTemplateId: 'tpl-1' });

  const staged = await stageSkillZip(request, makeZip('skill-content'), 's-1');

  assert.equal(staged.path, STAGED_PATH);
  assert.equal(fetchCalls.length, 1);
  const upload = fetchCalls[0];
  assert.equal(upload.url, '/file-api/upload-obs');
  assert.equal(upload.method, 'POST');
  assert.equal(upload.headers.get('X-Agent-Template-Id'), 'tpl-1');
  assert.ok(upload.headers.get('X-Request-Id'));
  const uploadBody = JSON.parse(upload.body);
  assert.equal(uploadBody.filename, 'demo.zip');
  assert.equal(Buffer.from(uploadBody.content_base64, 'base64').toString(), 'skill-content');

  assert.equal(calls.length, 1);
  assert.equal(calls[0].method, 'file.import_url');
  assert.deepEqual(calls[0].params, {
    session_id: 's-1',
    files: [{ url: OBJECT_URL, name: 'demo.zip', type: 'document' }],
  });
  assert.deepEqual(calls[0].options, { timeoutMs: 120_000 });

  await staged.cleanup();
  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[1].method, 'DELETE');
  assert.deepEqual(JSON.parse(fetchCalls[1].body), { url: OBJECT_URL });
});

test('安装失败时清理 OBS 临时对象并透出原错误', async () => {
  const fetchCalls = stubFetch((call) =>
    call.method === 'DELETE' ? new Response('{}', { status: 200 }) : uploadOk(),
  );
  const { request } = stubRequest(new Error('session expired'));

  await assert.rejects(stageSkillZip(request, makeZip(), 's-1'), /session expired/);
  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[1].method, 'DELETE');
  assert.deepEqual(JSON.parse(fetchCalls[1].body), { url: OBJECT_URL });
});

test('安装回包缺少路径时清理 OBS 临时对象', async () => {
  const fetchCalls = stubFetch((call) =>
    call.method === 'DELETE' ? new Response('{}', { status: 200 }) : uploadOk(),
  );
  const { request } = stubRequest({ files: [] });

  await assert.rejects(stageSkillZip(request, makeZip(), 's-1'), /did not return a staged path/);
  assert.equal(fetchCalls.length, 2);
  assert.equal(fetchCalls[1].method, 'DELETE');
});

test('cleanup 删除失败不阻断安装结果', async () => {
  stubFetch((call) => {
    if (call.method === 'DELETE') throw new Error('network down');
    return uploadOk();
  });
  const { request } = stubRequest({ files: [{ path: STAGED_PATH }] });

  const staged = await stageSkillZip(request, makeZip(), 's-1');
  await assert.doesNotReject(staged.cleanup());
});
