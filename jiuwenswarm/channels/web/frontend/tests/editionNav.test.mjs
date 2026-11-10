import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import {
  ENTERPRISE_HIDDEN_NAV_ITEMS,
  PERSONAL_HIDDEN_NAV_ITEMS,
} from '../node_modules/.cache/edition-nav/edition.mjs';

test('personal edition hides the left team page and enterprise keeps it', () => {
  assert.deepEqual([...PERSONAL_HIDDEN_NAV_ITEMS], ['teams']);
  assert.equal(ENTERPRISE_HIDDEN_NAV_ITEMS.includes('teams'), false);
});

test('the shell applies the personal hide list and does not mount the team page', () => {
  const app = readFileSync(new URL('../src/App.tsx', import.meta.url), 'utf8');
  assert.match(app, /isEnterprise\(\) \? ENTERPRISE_HIDDEN_NAV_ITEMS : PERSONAL_HIDDEN_NAV_ITEMS/);
  assert.match(app, /\{isEnterprise\(\) && activeNav === 'teams' && \(/);
});
