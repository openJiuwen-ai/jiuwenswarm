import assert from 'node:assert/strict';
import test from 'node:test';

import {
  DEFAULT_FRONTEND_PLATFORM,
  getHiddenNavItemsForPlatform,
  normalizeFrontendPlatform,
  resolveFrontendPlatform,
} from '../node_modules/.cache/frontend-platform/utils/frontendPlatform.js';

test('normalizeFrontendPlatform accepts only explicit supported platform names', () => {
  assert.equal(normalizeFrontendPlatform('harmony'), 'harmony');
  assert.equal(normalizeFrontendPlatform('Harmony'), 'harmony');
  assert.equal(normalizeFrontendPlatform('web'), 'web');
  assert.equal(normalizeFrontendPlatform('desktop'), 'web');
  assert.equal(normalizeFrontendPlatform('default'), null);
  assert.equal(normalizeFrontendPlatform('production'), null);
  assert.equal(normalizeFrontendPlatform(''), null);
  assert.equal(normalizeFrontendPlatform(undefined), null);
});

test('resolveFrontendPlatform defaults to web when no explicit source matches', () => {
  assert.equal(DEFAULT_FRONTEND_PLATFORM, 'web');
  assert.equal(resolveFrontendPlatform(), 'web');
  assert.equal(resolveFrontendPlatform(undefined, '', 'production', 'default'), 'web');
});

test('resolveFrontendPlatform uses the first explicit supported source', () => {
  assert.equal(resolveFrontendPlatform('harmony', 'web'), 'harmony');
  assert.equal(resolveFrontendPlatform(undefined, 'web', 'harmony'), 'web');
  assert.equal(resolveFrontendPlatform('default', 'harmony'), 'harmony');
});

test('getHiddenNavItemsForPlatform preserves current web behavior and trims Harmony sidebar', () => {
  assert.deepEqual(getHiddenNavItemsForPlatform('web'), ['sessions']);
  assert.deepEqual(getHiddenNavItemsForPlatform('harmony'), ['sessions', 'updatepanel']);
});
