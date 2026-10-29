import './fixtures/browserEnvironment.mjs';
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
  const positions = [{ name: 'demo-skill', offset: 9 }, { name: 'other-skill', offset: 16 }];
  useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, '/persist ship it', positions);
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
  assert.deepEqual(useChatStore.getState().getRuntime(NEW_CONVERSATION_ID)?.inputSkillPositions, positions);
});

test('replacing or sending draft text clears old skill positions', () => {
  const store = useChatStore.getState();
  store.ensureRuntime('draft-position-test');
  const positions = [{ name: 'demo-skill', offset: 5 }];
  try {
    store.setInputValue('draft-position-test', 'hello', positions);
    store.setInputValue('draft-position-test', 'hello');
    assert.deepEqual(store.getRuntime('draft-position-test').inputSkillPositions, positions);
    store.setInputValue('draft-position-test', 'replacement');
    assert.deepEqual(store.getRuntime('draft-position-test').inputSkillPositions, []);
    store.setInputValue('draft-position-test', 'hello', positions);
    store.setInputValue('draft-position-test', '');
    assert.deepEqual(store.getRuntime('draft-position-test').inputSkillPositions, []);
  } finally {
    store.removeRuntime('draft-position-test');
  }
});

test('empty drafts preserve the order of skill-only chips when reset', () => {
  const positions = [{ name: 'demo-skill', offset: 0 }, { name: 'other-skill', offset: 0 }];
  useChatStore.getState().setInputValue(NEW_CONVERSATION_ID, '', positions);
  resetNewConversationRuntime({ mode: 'agent', selectedModelName: null });
  assert.deepEqual(useChatStore.getState().getRuntime(NEW_CONVERSATION_ID).inputSkillPositions, positions);
});
