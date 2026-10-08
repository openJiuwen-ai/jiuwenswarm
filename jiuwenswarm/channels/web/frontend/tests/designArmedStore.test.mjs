import assert from 'node:assert/strict';
import test from 'node:test';

import { useDesignArmedStore } from '../node_modules/.cache/design-armed-store/designArmedStore.mjs';

test('design armed store is per-session and consumeArmed is one-shot', () => {
  useDesignArmedStore.setState({ runtimes: {} });

  assert.equal(useDesignArmedStore.getState().isArmed('new'), false);
  useDesignArmedStore.getState().setArmed('new', true);
  assert.equal(useDesignArmedStore.getState().isArmed('new'), true);
  assert.equal(useDesignArmedStore.getState().isArmed('sess_other'), false);

  assert.equal(useDesignArmedStore.getState().consumeArmed('new'), true);
  assert.equal(useDesignArmedStore.getState().isArmed('new'), false);
  assert.equal(useDesignArmedStore.getState().consumeArmed('new'), false);
});
