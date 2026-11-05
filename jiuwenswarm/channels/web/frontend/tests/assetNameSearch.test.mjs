import test from 'node:test';
import assert from 'node:assert/strict';
import { matchesAssetName } from '../node_modules/.cache/asset-name-search/assetNameSearch.js';

test('visible names support partial, case-insensitive and normalized matching', () => {
  for (const query of ['case', ' CASEWORK ', 'ＣＡＳＥＷＯＲＫ', '', '  ']) {
    assert.equal(matchesAssetName(query, ['Casework Organizer']), true, query);
  }
  assert.equal(matchesAssetName('案件', ['案件整理顾问']), true);
  assert.equal(matchesAssetName('s', ['案件整理顾问']), false);
  assert.equal(matchesAssetName('Casework', ['案件整理顾问']), false);
  assert.equal(matchesAssetName('alpha beta', ['Alpha', 'Beta']), false);
});
