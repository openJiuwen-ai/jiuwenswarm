import assert from 'node:assert/strict';
import test from 'node:test';

import {
  isComposerSlashChipCommand,
  splitComposerSlashChip,
} from '../node_modules/.cache/composer-draft-hydrate/composerDraftHydrate.mjs';

test('only parameterized draft slash commands become chips (#5771)', () => {
  assert.equal(isComposerSlashChipCommand('persist'), true);
  assert.equal(isComposerSlashChipCommand('Persist'), true);
  assert.equal(isComposerSlashChipCommand('plan'), false);
  assert.equal(isComposerSlashChipCommand('fork'), false);
});

test('splitComposerSlashChip restores /persist chip + remaining args', () => {
  assert.deepEqual(splitComposerSlashChip('/persist follow release'), {
    command: 'persist',
    rest: ' follow release',
  });
  assert.deepEqual(splitComposerSlashChip('/persist'), {
    command: 'persist',
    rest: '',
  });
  assert.deepEqual(splitComposerSlashChip('/plan do stuff'), {
    command: null,
    rest: '/plan do stuff',
  });
  assert.deepEqual(splitComposerSlashChip('hello /persist'), {
    command: null,
    rest: 'hello /persist',
  });
});
