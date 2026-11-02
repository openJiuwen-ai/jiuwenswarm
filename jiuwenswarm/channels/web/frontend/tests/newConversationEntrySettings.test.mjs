import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveNewConversationEntrySettings } from '../node_modules/.cache/new-conversation-entry-settings/newConversationLifecycle.mjs';

test('returning to an unsent new conversation keeps its mode and model', () => {
  assert.deepEqual(
    resolveNewConversationEntrySettings('agent', 'main-model', 'other-model', {
      mode: 'team',
      selectedModelName: 'draft-model',
    }),
    { mode: 'team', selectedModelName: 'draft-model' },
  );
});

test('starting a new conversation still uses the configured default model', () => {
  assert.deepEqual(
    resolveNewConversationEntrySettings('team', 'main-model', 'other-model'),
    { mode: 'team', selectedModelName: 'main-model' },
  );
});

test('new conversation keeps the current model only while the model list is loading', () => {
  assert.deepEqual(
    resolveNewConversationEntrySettings('agent', null, 'other-model'),
    { mode: 'agent', selectedModelName: 'other-model' },
  );
});
