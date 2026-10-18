import test from 'node:test';
import assert from 'node:assert/strict';
import { assetSearchNames, matchesAssetName } from '../node_modules/.cache/asset-name-search/assetNameSearch.js';
test('display names only, with bilingual and normalized matching', () => {
    const names = assetSearchNames({ zh: '旅游规划', en: 'Travel Team' }, 'hidden-s-6');
    for (const q of ['旅游', ' TRAVEL ', 'Ｔｒａｖｅｌ', '', '  ']) assert.equal(matchesAssetName(q, names), true, q);
    for (const q of ['hidden', '生成', 's', '6']) assert.equal(matchesAssetName(q, names), false, q);
});
test('fallback is searchable only when no display name is available', () => {
  assert.deepEqual(assetSearchNames({ zh: ' ', en: '' }, 'sample-group'), ['sample-group']);
  assert.equal(matchesAssetName('sample', assetSearchNames(undefined, 'sample-group')), true);
  assert.equal(matchesAssetName('sample', assetSearchNames('展示名', 'sample-group')), false);
  assert.equal(matchesAssetName('alpha beta', ['Alpha', 'Beta']), false);
});
