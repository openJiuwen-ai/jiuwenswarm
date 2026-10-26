import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveAuthorizationScope } from '../node_modules/.cache/authorization-scope/components/InteractionSlot/authorizationScope.js';

for (const scope of ['parent', 'domain', 'subcommands']) {
  test(`defaults to ${scope}, while preserving an explicit exact choice`, () => {
    const options = [{ value: 'exact' }, { value: scope }];
    assert.equal(resolveAuthorizationScope(options), scope);
    assert.equal(resolveAuthorizationScope(options, 'exact'), 'exact');
    assert.equal(resolveAuthorizationScope(options, scope), scope);
  });
}

test('missing or exact-only metadata stays exact for unsupported operations and older servers', () => {
  assert.equal(resolveAuthorizationScope(undefined), 'exact');
  assert.equal(resolveAuthorizationScope([]), 'exact');
  assert.equal(resolveAuthorizationScope([{ value: 'exact' }], 'subcommands'), 'exact');
});

test('a selection from another object type cannot replace the current available default', () => {
  assert.equal(resolveAuthorizationScope([{ value: 'exact' }, { value: 'domain' }], 'parent'), 'domain');
});
