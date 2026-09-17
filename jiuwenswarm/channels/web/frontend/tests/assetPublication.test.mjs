import test from 'node:test';
import assert from 'node:assert/strict';
import {
  publicationDetailLabel,
  matchesPublicationFilter,
} from '../node_modules/.cache/asset-publication/features/assetPublication.js';
test('unconfirmed and unavailable statuses cannot be filtered as unpublished', () => {
  for (const state of ['pending', 'unknown', 'loading']) {
    assert.equal(matchesPublicationFilter(state, 'unpublished'), false);
    assert.equal(matchesPublicationFilter(state, 'all'), true);
  }
  assert.equal(matchesPublicationFilter('unpublished', 'unpublished'), true);
  assert.equal(matchesPublicationFilter('published', 'published'), true);
});


test('expert, plugin and MCP details omit uncertain labels but keep confirmed states', () => {
  for (const language of ['zh', 'en']) {
    assert.equal(publicationDetailLabel('unknown', language), '');
    assert.equal(publicationDetailLabel('loading', language), '');
  }
  assert.equal(publicationDetailLabel('unpublished', 'zh'), '未发布');
  assert.equal(publicationDetailLabel('published', 'zh'), '已发布');
  assert.equal(publicationDetailLabel('pending', 'zh'), '待审核');
});

test('pending review has its own filter and excludes unknown states', () => {
  assert.equal(matchesPublicationFilter('pending', 'pending'), true);
  for (const state of ['unknown', 'loading', 'published', 'unpublished']) assert.equal(matchesPublicationFilter(state, 'pending'), false);
});
