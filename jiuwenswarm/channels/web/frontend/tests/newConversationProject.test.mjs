import assert from 'node:assert/strict';
import test from 'node:test';

import { resolveNewConversationProjectDir } from '../node_modules/.cache/new-conversation-project/multi-session/state/newConversationProject.js';

test('ordinary new conversations do not inherit a selected project', () => {
  assert.equal(
    resolveNewConversationProjectDir(false, undefined, '/workspace/old-project'),
    null,
  );
});

test('project-specific new conversations keep the explicit project', () => {
  assert.equal(
    resolveNewConversationProjectDir(true, '/workspace/selected-project', '/workspace/old-project'),
    '/workspace/selected-project',
  );
});

test('project-specific new conversations fall back to the selected project', () => {
  assert.equal(
    resolveNewConversationProjectDir(true, undefined, '/workspace/selected-project'),
    '/workspace/selected-project',
  );
});
