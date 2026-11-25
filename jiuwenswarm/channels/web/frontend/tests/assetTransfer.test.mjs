import assert from 'node:assert/strict';
import test from 'node:test';
import {
  AssetTransferError,
  assetIssueKey,
  exportAssetToLocalFile,
  importAssetFromFile,
  requestAssetExport,
  uploadAssetPackage,
} from '../node_modules/.cache/asset-transfer/assetTransfer.mjs';

const EXPORT_INFO = {
  download_url: '/file-api/download?token=tok&user_id=u1',
  filename: 'expert-1-1.2.0.zip',
  size_bytes: 9,
  sha256: 'a'.repeat(64),
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
