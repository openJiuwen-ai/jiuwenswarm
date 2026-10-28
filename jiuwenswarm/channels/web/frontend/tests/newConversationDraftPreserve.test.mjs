import assert from 'node:assert/strict';
import test from 'node:test';

import {
  NEW_CONVERSATION_ID,
  resetNewConversationRuntime,
  useChatStore,
  useSessionStore,
} from '../node_modules/.cache/new-conversation-draft-preserve/newConversationDraftPreserveExports.mjs';

test('resetNewConversationRuntime keeps draft skills and slash text (#5771)', () => {
  useChatStore.getState().ensureRuntime(NEW_CONVERSATION_ID);
  useSessionStore.getState().ensureRuntime(NEW_CONVERSATION_ID);
  useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, '/persist ship it');
  useSessionStore.getState().addSelectedSkill(NEW_CONVERSATION_ID, 'demo-skill');
  useSessionStore.getState().addSelectedSkill(NEW_CONVERSATION_ID, 'other-skill');
  useSessionStore.getState().setAgentSelectionIntent(NEW_CONVERSATION_ID, {
    kind: 'select',
    id: 'expert-a',
  });

  resetNewConversationRuntime({
    mode: 'agent',
    selectedModelName: null,
    selectedModelSelection: null,
  });

  const sessionRuntime = useSessionStore.getState().getRuntime(NEW_CONVERSATION_ID);
  assert.deepEqual(sessionRuntime.selectedSkills, ['demo-skill', 'other-skill']);
  assert.deepEqual(sessionRuntime.agentSelectionIntent, { kind: 'select', id: 'expert-a' });
  assert.equal(useChatStore.getState().getRuntime(NEW_CONVERSATION_ID)?.inputValue, '/persist ship it');
});
