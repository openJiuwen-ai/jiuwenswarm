import assert from 'node:assert/strict';
import test from 'node:test';

import {
  findOverlappingFileExecutionEvent,
  getFileIdentityKey,
  mergeFileDownloadItems,
  resolveFilePath,
} from '../node_modules/.cache/file-download-dedup/utils/fileDownloadDedup.js';

function makeToken(path, exp = 9_999_999_999) {
  const payload = Buffer.from(JSON.stringify({ path, exp, sid: 's1' }), 'utf8').toString('base64url');
  return `${payload}.fakesig`;
}

test('identity prefers explicit path over name/size', () => {
  assert.equal(
    getFileIdentityKey({ path: 'E:\\ws\\a.pdf', name: 'a.pdf', size: 1 }),
    'path:E:/ws/a.pdf'
  );
  assert.equal(
    getFileIdentityKey({ name: 'a.pdf', size: 12 }),
    'name:a.pdf|size:12'
  );
});

test('identity resolves path from download token payload', () => {
  const token = makeToken('/workspace/report.md');
  assert.equal(
    getFileIdentityKey({
      name: 'report.md',
      download_url: `/file-api/download?token=${encodeURIComponent(token)}`,
    }),
    'path:/workspace/report.md'
  );
  assert.equal(resolveFilePath({ download_token: token }), '/workspace/report.md');
});

test('merge refreshes download url for same path identity', () => {
  const token1 = makeToken('/ws/a.txt', 100);
  const token2 = makeToken('/ws/a.txt', 200);
  const merged = mergeFileDownloadItems(
    [{ name: 'a.txt', path: '/ws/a.txt', download_url: `/file-api/download?token=${token1}` }],
    [{ name: 'a.txt', path: '/ws/a.txt', download_url: `/file-api/download?token=${token2}` }]
  );
  assert.equal(merged.length, 1);
  assert.equal(merged[0].download_url, `/file-api/download?token=${token2}`);
});

test('merge keeps distinct files as separate items', () => {
  const merged = mergeFileDownloadItems(
    [{ name: 'a.pdf', path: '/ws/a.pdf', download_url: '/d?token=1' }],
    [{ name: 'b.pdf', path: '/ws/b.pdf', download_url: '/d?token=2' }]
  );
  assert.equal(merged.length, 2);
});

test('merge does not wipe download_url with explicit undefined', () => {
  const merged = mergeFileDownloadItems(
    [{ name: 'a.pdf', path: '/ws/a.pdf', download_url: '/d?token=keep', download_token: 'tok' }],
    [{ name: 'a.pdf', path: '/ws/a.pdf', download_url: undefined, download_token: 'tok2' }]
  );
  assert.equal(merged.length, 1);
  assert.equal(merged[0].download_url, '/d?token=keep');
  assert.equal(merged[0].download_token, 'tok2');
});

test('merge prefers incoming with url over existing without url', () => {
  const merged = mergeFileDownloadItems(
    [{ name: 'a.pdf', path: '/ws/a.pdf' }],
    [{ name: 'a.pdf', path: '/ws/a.pdf', download_url: '/d?token=new' }]
  );
  assert.equal(merged[0].download_url, '/d?token=new');
});

test('findOverlappingFileExecutionEvent only matches shared identity', () => {
  const events = [
    {
      id: 'e1',
      member_id: 'm1',
      kind: 'file',
      files: [{ name: 'a.pdf', path: '/ws/a.pdf' }],
    },
    {
      id: 'e2',
      member_id: 'm1',
      kind: 'file',
      files: [{ name: 'b.pdf', path: '/ws/b.pdf' }],
    },
  ];

  const hit = findOverlappingFileExecutionEvent(
    events,
    [{ name: 'a.pdf', path: '/ws/a.pdf', download_url: '/d?token=fresh' }],
    (event) => event.member_id === 'm1' && event.kind === 'file'
  );
  assert.equal(hit?.id, 'e1');

  const miss = findOverlappingFileExecutionEvent(
    events,
    [{ name: 'c.pdf', path: '/ws/c.pdf' }],
    (event) => event.member_id === 'm1' && event.kind === 'file'
  );
  assert.equal(miss, undefined);
});
