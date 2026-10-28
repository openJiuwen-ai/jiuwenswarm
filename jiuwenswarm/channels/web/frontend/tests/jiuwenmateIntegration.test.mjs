import assert from 'node:assert/strict';
import test from 'node:test';

import {
  JIUWENMATE_PUBLIC_PAGES,
  openJiuwenMatePublicPage,
  reenterJiuwenMate,
} from '../node_modules/.cache/jiuwenmate-integration/publicNav.js';

const EXISTING_PAGES = new Set(['agents', 'personalContext', 'skills', 'connectorMarket', 'settings']);

test('public rail entries open existing swarm pages and do not create jiuwenmate copies', () => {
  const byLabel = Object.fromEntries(JIUWENMATE_PUBLIC_PAGES.map((item) => [item.labelKey, item.nav]));
  assert.equal(byLabel['jiuwenmate.nav.plaza'], 'agents');
  assert.equal(byLabel['jiuwenmate.nav.assets'], 'personalContext');
  assert.equal(byLabel['jiuwenmate.nav.knowledge'], 'personalContext');
  assert.equal(byLabel['jiuwenmate.nav.skills'], 'skills');
  assert.equal(byLabel['jiuwenmate.nav.extensions'], 'connectorMarket');
  assert.equal(byLabel['jiuwenmate.nav.settings'], 'settings');
  assert.equal(JIUWENMATE_PUBLIC_PAGES.length, 6);
  for (const item of JIUWENMATE_PUBLIC_PAGES) {
    assert.equal(EXISTING_PAGES.has(item.nav), true);
  }
});

test('opening a reused page stays inside jiuwenmate and preserves the personal page', () => {
  const inside = { isJiuwenMate: true, activeNav: 'chat' };
  const opened = openJiuwenMatePublicPage(inside, 'skills');
  assert.deepEqual(opened, { isJiuwenMate: true, activeNav: 'chat', embeddedPage: 'skills' });

  const exited = { isJiuwenMate: false, activeNav: opened.activeNav };
  const returned = reenterJiuwenMate(exited);
  assert.deepEqual(returned, { isJiuwenMate: true, activeNav: 'chat' });
});
