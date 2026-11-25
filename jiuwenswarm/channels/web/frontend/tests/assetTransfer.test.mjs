import assert from 'node:assert/strict';
import test from 'node:test';
import { createHash } from 'node:crypto';
import {
  AssetTransferError,
  assetIssueKey,
  exportAssetToLocalFile,
  importAssetFromFile,
  importAssetFromPath,
  requestAssetExport,
  stageLocalAssetPackage,
  uploadAssetPackage,
} from '../node_modules/.cache/asset-transfer/assetTransfer.mjs';

// 与测试用 Blob 内容一致的真实摘要，供 sha256 校验通过路径使用。
const ZIP_SHA256 = createHash('sha256').update('zip-bytes').digest('hex');

const EXPORT_INFO = {
  download_url: '/file-api/download?token=tok&user_id=u1',
  filename: 'expert-1-1.2.0.zip',
  size_bytes: 9,
  sha256: ZIP_SHA256,
};

function fakeResponse(overrides = {}) {
  return { ok: true, status: 200, json: async () => ({}), blob: async () => new Blob(), ...overrides };
}

function zipFile(name = 'expert.zip') {
  return new File(['zip-bytes'], name, { type: 'application/zip' });
}

test('assets.export passes kind/local_id and omits version when absent', async () => {
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    return EXPORT_INFO;
  };
  const info = await requestAssetExport({ kind: 'agent_template', local_id: 'expert-1' }, { request });
  assert.deepEqual(info, EXPORT_INFO);
  assert.deepEqual(calls, [['assets.export', { kind: 'agent_template', local_id: 'expert-1' }]]);
  await requestAssetExport({ kind: 'skill', local_id: 's1', version: '2.0.0' }, { request });
  assert.deepEqual(calls[1], ['assets.export', { kind: 'skill', local_id: 's1', version: '2.0.0' }]);
});

test('export chain fetches the short-lived url and saves under the contract filename', async () => {
  const fetched = [];
  const saved = [];
  const blob = new Blob(['zip-bytes']);
  const result = await exportAssetToLocalFile(
    { kind: 'agent_group', local_id: 'team-1' },
    {
      request: async () => EXPORT_INFO,
      fetchImpl: async (url) => {
        fetched.push(url);
        return fakeResponse({ blob: async () => blob });
      },
      saveBlob: async (b, filename) => {
        saved.push([b, filename]);
        return { outcome: 'saved', transport: 'browser-download' };
      },
    },
  );
  assert.deepEqual(fetched, [EXPORT_INFO.download_url]);
  assert.deepEqual(saved, [[blob, 'expert-1-1.2.0.zip']]);
  assert.deepEqual(result, { outcome: 'saved', transport: 'browser-download' });
});

test('download failures converge to ASSET_DOWNLOAD_FAILED', async () => {
  const httpFail = exportAssetToLocalFile(
    { kind: 'skill', local_id: 's1' },
    { request: async () => EXPORT_INFO, fetchImpl: async () => fakeResponse({ ok: false, status: 403 }) },
  );
  await assert.rejects(
    httpFail,
    (err) => err instanceof AssetTransferError && err.code === 'ASSET_DOWNLOAD_FAILED' && err.status === 403,
  );

  const netFail = exportAssetToLocalFile(
    { kind: 'skill', local_id: 's1' },
    {
      request: async () => EXPORT_INFO,
      fetchImpl: async () => {
        throw new Error('network down');
      },
    },
  );
  await assert.rejects(netFail, /network down/);
  assert.equal(assetIssueKey(await netFail.catch((e) => e)), 'assetTransfer.errors.downloadFailed');
});

test('sha256 check: mismatch rejects; malformed/missing digest or absent crypto.subtle skip', async () => {
  const blob = new Blob(['zip-bytes']);
  const depsFor = (info, saved) => ({
    request: async () => info,
    fetchImpl: async () => fakeResponse({ blob: async () => blob }),
    saveBlob: async () => {
      saved.push(blob);
      return { outcome: 'saved', transport: 'browser-download' };
    },
  });

  // 摘要不一致 → 前端本地码 ASSET_CHECKSUM_MISMATCH，不落盘
  const mismatch = exportAssetToLocalFile(
    { kind: 'skill', local_id: 's1' },
    depsFor({ ...EXPORT_INFO, sha256: 'b'.repeat(64) }, []),
  );
  await assert.rejects(
    mismatch,
    (err) => err instanceof AssetTransferError && err.code === 'ASSET_CHECKSUM_MISMATCH',
  );
  assert.equal(assetIssueKey(await mismatch.catch((e) => e)), 'assetTransfer.errors.checksumMismatch');

  // 非法形态（非 64 位十六进制）→ 跳过校验正常保存
  const savedMalformed = [];
  await exportAssetToLocalFile(
    { kind: 'skill', local_id: 's1' },
    depsFor({ ...EXPORT_INFO, sha256: 'x' }, savedMalformed),
  );
  assert.equal(savedMalformed.length, 1);

  // 摘要缺失（契约中 sha256 可选）→ 跳过校验正常保存
  const savedMissing = [];
  await exportAssetToLocalFile(
    { kind: 'skill', local_id: 's1' },
    depsFor(
      {
        download_url: EXPORT_INFO.download_url,
        filename: EXPORT_INFO.filename,
        size_bytes: EXPORT_INFO.size_bytes,
      },
      savedMissing,
    ),
  );
  assert.equal(savedMissing.length, 1);

  // 无 crypto.subtle（纯 HTTP 部署的非安全上下文）→ 优雅降级，不阻断下载
  const originalCrypto = globalThis.crypto;
  Object.defineProperty(globalThis, 'crypto', { value: undefined, configurable: true });
  try {
    const savedNoSubtle = [];
    await exportAssetToLocalFile({ kind: 'skill', local_id: 's1' }, depsFor(EXPORT_INFO, savedNoSubtle));
    assert.equal(savedNoSubtle.length, 1);
  } finally {
    Object.defineProperty(globalThis, 'crypto', { value: originalCrypto, configurable: true });
  }
});

test('upload success returns the staged server path', async () => {
  const captured = [];
  const path = await uploadAssetPackage(zipFile(), {
    fetchImpl: async (url, init) => {
      captured.push([url, init]);
      return fakeResponse({ json: async () => ({ path: '/data/uploads/assets/x.zip' }) });
    },
  });
  assert.equal(path, '/data/uploads/assets/x.zip');
  assert.equal(captured[0][0], '/file-api/assets/upload-temp');
  assert.equal(captured[0][1].method, 'POST');
  assert.equal(captured[0][1].body.get('file').name, 'expert.zip');
  assert.equal(captured[0][1].body.get('file').size, 'zip-bytes'.length);
});

test('upload rejection carries the server contract code and status', async () => {
  const promise = uploadAssetPackage(zipFile(), {
    fetchImpl: async () => fakeResponse({
      ok: false,
      status: 413,
      json: async () => ({ code: 'ASSET_LIMIT_EXCEEDED', message: 'upload too large', error: 'upload too large' }),
    }),
  });
  await assert.rejects(
    promise,
    (err) => err.code === 'ASSET_LIMIT_EXCEEDED' && err.status === 413 && err.message === 'upload too large',
  );
  assert.equal(assetIssueKey(await promise.catch((e) => e)), 'assetTransfer.errors.limitExceeded');
});

test('non-JSON upload response falls back to ASSET_UPLOAD_FAILED', async () => {
  const promise = uploadAssetPackage(zipFile(), {
    fetchImpl: async () => fakeResponse({
      ok: false,
      status: 502,
      json: async () => {
        throw new Error('bad json');
      },
    }),
  });
  await assert.rejects(promise, (err) => err.code === 'ASSET_UPLOAD_FAILED' && err.status === 502);
  assert.equal(assetIssueKey(await promise.catch((e) => e)), 'assetTransfer.errors.uploadFailed');
});

test('import chain: upload then assets.import with optional kind', async () => {
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    return { kind: 'agent_group', id: 'team-1', pending_connectors: ['crm'] };
  };
  const fetchImpl = async () => fakeResponse({ json: async () => ({ path: '/data/uploads/assets/y.zip' }) });
  const sniffed = await importAssetFromFile(zipFile(), undefined, { request, fetchImpl });
  assert.deepEqual(calls[0], ['assets.import', { path: '/data/uploads/assets/y.zip' }]);
  assert.deepEqual(sniffed, { kind: 'agent_group', id: 'team-1', pending_connectors: ['crm'] });
  await importAssetFromFile(zipFile(), 'agent_template', { request, fetchImpl });
  assert.deepEqual(calls[1], ['assets.import', { path: '/data/uploads/assets/y.zip', kind: 'agent_template' }]);
});

test('importAssetFromPath calls assets.import directly with no upload fetch', async () => {
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    return { kind: 'agent_template', id: 'expert-1' };
  };
  const fetchImpl = async () => {
    throw new Error('upload must not be triggered for a staged path');
  };
  const explicit = await importAssetFromPath('/data/uploads/assets/z.zip', 'agent_template', { request, fetchImpl });
  assert.deepEqual(explicit, { kind: 'agent_template', id: 'expert-1' });
  assert.deepEqual(calls[0], ['assets.import', { path: '/data/uploads/assets/z.zip', kind: 'agent_template' }]);
  // kind 缺省时不带 kind 字段，由服务端按适配器注册顺序嗅探。
  await importAssetFromPath('/data/uploads/assets/z.zip', undefined, { request, fetchImpl });
  assert.deepEqual(calls[1], ['assets.import', { path: '/data/uploads/assets/z.zip' }]);
});

test('stageLocalAssetPackage swaps a desktop local path into the staged root', async () => {
  const calls = [];
  const request = async (method, params) => {
    calls.push([method, params]);
    return { path: '/data/uploads/assets/local.zip' };
  };
  const fetchImpl = async () => {
    throw new Error('HTTP upload must not be triggered for a local path');
  };
  const staged = await stageLocalAssetPackage('/home/user/pack.zip', { request, fetchImpl });
  assert.equal(staged, '/data/uploads/assets/local.zip');
  assert.deepEqual(calls, [['assets.stage_local', { path: '/home/user/pack.zip' }]]);
  // 响应缺 path → 本地兜底码（与 upload 非 JSON 响应同一兜底）
  await assert.rejects(
    stageLocalAssetPackage('/home/user/pack.zip', { request: async () => ({}) }),
    (err) => err instanceof AssetTransferError && err.code === 'ASSET_UPLOAD_FAILED',
  );
});

test('eight contract codes map to i18n keys; unknown codes fall back to requestFailed', () => {
  const expected = {
    ASSET_NOT_FOUND: 'notFound',
    ASSET_EXPORT_FORBIDDEN: 'exportForbidden',
    ASSET_INVALID_PACKAGE: 'invalidPackage',
    ASSET_UNSAFE_PATH: 'unsafePath',
    ASSET_CREDENTIAL_DETECTED: 'credentialDetected',
    ASSET_LIMIT_EXCEEDED: 'limitExceeded',
    ASSET_ALREADY_EXISTS: 'alreadyExists',
    ASSET_BUILTIN_READ_ONLY: 'builtinReadOnly',
  };
  for (const [code, suffix] of Object.entries(expected)) {
    assert.equal(assetIssueKey(code), `assetTransfer.errors.${suffix}`);
    assert.equal(assetIssueKey({ code }), `assetTransfer.errors.${suffix}`);
  }
  assert.equal(assetIssueKey('SOMETHING_ELSE'), 'assetTransfer.errors.requestFailed');
  assert.equal(assetIssueKey(undefined), 'assetTransfer.errors.requestFailed');
  // WebError.payload 透传的失败 payload（{error, code}）同样能取出码
  assert.equal(
    assetIssueKey({ payload: { error: 'exists', code: 'ASSET_ALREADY_EXISTS' } }),
    'assetTransfer.errors.alreadyExists',
  );
});
