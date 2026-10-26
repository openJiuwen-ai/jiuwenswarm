import assert from 'node:assert/strict';
import test from 'node:test';

import { buildArtifacts, fileArtifactId } from '../node_modules/.cache/artifact-collection/artifactCollection.mjs';

function file(overrides = {}) {
  return {
    name: 'report.txt',
    size: 10,
    mime_type: 'text/plain',
    download_url: '/file-api/download?token=report',
    download_token: 'report',
    ...overrides,
  };
}

function message(role, id, timestamp, fileItems) {
  return {
    id,
    role,
    content: '',
    timestamp,
    fileItems,
  };
}

test('collects files from every message role', () => {
  const artifacts = buildArtifacts([
    message('user', 'user-message', '2026-07-30T08:00:00.000Z', [file({ name: 'input.txt' })]),
    message('system', 'team-message', '2026-07-30T09:00:00.000Z', [file({ name: 'team-draft.txt' })]),
    message('tool', 'tool-message', '2026-07-30T10:00:00.000Z', [file({ name: 'tool-output.txt' })]),
    message('assistant', 'assistant-message', '2026-07-30T11:00:00.000Z', [file({ name: 'final.txt' })]),
  ]);

  assert.deepEqual(
    artifacts.map(artifact => artifact.name),
    ['final.txt', 'tool-output.txt', 'team-draft.txt', 'input.txt'],
  );
  assert.ok(artifacts.every(artifact => artifact.source === 'message'));
});

test('rejects file entries without a display name or readable resource', () => {
  const artifacts = buildArtifacts([
    message('assistant', 'assistant-message', '2026-07-30T11:00:00.000Z', [
      file({ name: '   ' }),
      file({ name: 'missing-resource.txt', download_url: '', download_token: '' }),
      file({ name: 'signed-url.txt', download_url: '/file-api/download?token=url', download_token: '' }),
      file({ name: 'token-only.txt', download_url: '', download_token: 'token-only' }),
      file({ name: 'path-only.txt', download_url: '', download_token: '', path: 'agent/workspace/path-only.txt' }),
    ]),
  ]);

  assert.deepEqual(artifacts.map(artifact => artifact.name).sort(), ['path-only.txt', 'signed-url.txt', 'token-only.txt']);
  assert.equal(artifacts.find(artifact => artifact.name === 'token-only.txt')?.downloadUrl, '/file-api/download?token=token-only');
});

test('sorts assistant artifacts newest first and keeps card selection ids stable', () => {
  const olderFile = file({ name: ' older.txt ', download_token: 'older', download_url: '' });
  const artifacts = buildArtifacts([
    message('assistant', 'older', '2026-07-30T08:00:00.000Z', [olderFile]),
    message('assistant', 'newer', '2026-07-30T12:00:00.000Z', [file({ name: 'newer.txt', download_token: 'newer', download_url: '' })]),
  ]);

  assert.deepEqual(
    artifacts.map(artifact => artifact.name),
    ['newer.txt', 'older.txt'],
  );
  assert.equal(artifacts[1].id, fileArtifactId(olderFile));
});

test('duplicate file cards select the retained artifact with the same stable id', () => {
  const olderFile = file({
    path: '/workspace/report.txt',
    download_url: '/file-api/download?token=older',
    download_token: 'older',
  });
  const newerFile = file({
    path: '/workspace/report.txt',
    download_url: '/file-api/download?token=newer',
    download_token: 'newer',
  });

  const artifacts = buildArtifacts([
    message('assistant', 'older-message', '2026-07-30T08:00:00.000Z', [olderFile]),
    message('assistant', 'newer-message', '2026-07-30T12:00:00.000Z', [newerFile]),
  ]);

  assert.equal(artifacts.length, 1);
  assert.equal(fileArtifactId(olderFile), fileArtifactId(newerFile));
  assert.equal(artifacts[0].id, fileArtifactId(olderFile));
  assert.equal(artifacts[0].downloadUrl, newerFile.download_url);
});
