import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  errorToMessage,
  resolveUploadErrorMessage,
} from '../node_modules/.cache/skill-upload-errors/skillUploadErrors.mjs';

function fakeT(key, params) {
  if (!params || Object.keys(params).length === 0) return key;
  return `${key}|${JSON.stringify(params)}`;
}

test('errorToMessage prefers detailKey with params when t is provided', () => {
  const err = Object.assign(new Error('fallback'), {
    detailKey: 'skills.errors.uploadZipDepthExceeded',
    detailParams: { limit: 20 },
  });
  assert.equal(
    errorToMessage(err, fakeT),
    'skills.errors.uploadZipDepthExceeded|{"limit":20}',
  );
});

test('errorToMessage falls back to Error.message without detailKey', () => {
  assert.equal(errorToMessage(new Error('plain upload failure'), fakeT), 'plain upload failure');
});

test('overwrite conflict from a WebError payload follows the UI language', () => {
  const error = Object.assign(new Error('skill demo 已存在，需确认覆盖'), {
    payload: {
      detail_key: 'skills.errors.importOverwriteRequired',
      detail_params: { name: 'demo' },
    },
  });
  const expected = {
    en: 'Skill "demo" already exists and requires overwrite confirmation',
    zh: '技能「demo」已存在，需确认覆盖',
  };
  for (const [language, message] of Object.entries(expected)) {
    const path = new URL(`../src/i18n/locales/${language}.json`, import.meta.url);
    const locale = JSON.parse(readFileSync(path, 'utf8'));
    const t = (key, params) => key.split('.').reduce((value, part) => value[part], locale)
      .replace('{{name}}', params.name);
    assert.equal(errorToMessage(error, t), message);
  }
});

test('resolveUploadErrorMessage uses detailKey first', () => {
  assert.equal(
    resolveUploadErrorMessage(
      'SKILL_FILE_TOO_LARGE',
      413,
      'skills.errors.skillPackageTooLarge',
      { sizeMB: '60.0', limitMB: '50.0' },
      fakeT,
    ),
    'skills.errors.skillPackageTooLarge|{"sizeMB":"60.0","limitMB":"50.0"}',
  );
});

test('resolveUploadErrorMessage maps 413 with size/limit to skillPackageTooLarge', () => {
  assert.equal(
    resolveUploadErrorMessage(
      undefined,
      413,
      undefined,
      { sizeMB: '60.0', limitMB: '50.0' },
      fakeT,
    ),
    'skills.errors.skillPackageTooLarge|{"sizeMB":"60.0","limitMB":"50.0"}',
  );
});

test('resolveUploadErrorMessage falls back to uploadFailed when 413 lacks params', () => {
  assert.equal(
    resolveUploadErrorMessage('SKILL_FILE_TOO_LARGE', 413, undefined, undefined, fakeT),
    'skills.errors.uploadFailed',
  );
  assert.equal(
    resolveUploadErrorMessage(undefined, 413, undefined, { sizeMB: '60.0' }, fakeT),
    'skills.errors.uploadFailed',
  );
});

test('resolveUploadErrorMessage maps 400 unsafe/invalid package codes', () => {
  assert.equal(
    resolveUploadErrorMessage('SKILL_UNSAFE_PATH', 400, undefined, undefined, fakeT),
    'skills.errors.uploadUnsafePath',
  );
  assert.equal(
    resolveUploadErrorMessage('SKILL_INVALID_PACKAGE', 400, undefined, undefined, fakeT),
    'skills.errors.uploadInvalidRequest',
  );
});

test('resolveUploadErrorMessage defaults to uploadFailed', () => {
  assert.equal(
    resolveUploadErrorMessage('OTHER', 500, undefined, undefined, fakeT),
    'skills.errors.uploadFailed',
  );
});
