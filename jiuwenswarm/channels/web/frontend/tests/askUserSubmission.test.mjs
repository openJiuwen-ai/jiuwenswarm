import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildEmptyAskUserAnswers,
  hasAskUserInput,
  isIncompleteAskUserPage,
  resolveAskUserStatus,
  shouldUnlockPendingApproval,
} from '../node_modules/.cache/ask-user-submission/components/InteractionSlot/interactionSubmission.js';

const choiceQuestion = { question: 'Choose?', options: [{ label: 'A' }, { label: 'B' }, { label: 'Other' }], multi_select: true };
const emptyChoice = { selected: [], custom: '', customActive: false };

test('clearing the default multi-selection requires an answer without restoring the default', () => {
  const state = { ...emptyChoice, selected: ['A'] };
  assert.equal(isIncompleteAskUserPage(choiceQuestion, state), false);
  state.selected = [];
  assert.equal(isIncompleteAskUserPage(choiceQuestion, state), true);
  assert.deepEqual(state.selected, []);
  assert.equal(isIncompleteAskUserPage(choiceQuestion, { ...state, custom: 'My answer' }), false);
  assert.equal(isIncompleteAskUserPage(choiceQuestion, { ...state, custom: '  ' }), true);
});

test('empty or whitespace Other requires input for single-choice questions', () => {
  const question = { ...choiceQuestion, multi_select: false };
  for (const custom of ['', '  ']) {
    assert.equal(isIncompleteAskUserPage(question, { ...emptyChoice, customActive: true, custom }), true);
  }
  assert.equal(isIncompleteAskUserPage(question, { ...emptyChoice, customActive: true, custom: 'Answer' }), false);
});

test('explicit skip permits an empty answer and does not select a default', () => {
  const skipped = { ...emptyChoice, skippedNoSelection: true };
  assert.equal(isIncompleteAskUserPage(choiceQuestion, skipped), false);
  assert.deepEqual(skipped.selected, []);
});

test('submission validation detects an incomplete earlier page', () => {
  const states = [{ ...emptyChoice, customActive: true }, { ...emptyChoice, selected: ['B'] }];
  assert.equal(states.findIndex(state => isIncompleteAskUserPage(choiceQuestion, state)), 0);
});

test('free-input questions retain their existing empty-input behavior', () => {
  assert.equal(isIncompleteAskUserPage({ ...choiceQuestion, options: [] }, emptyChoice), false);
});

test('preserves explicit answered even when answers are empty', () => {
  assert.equal(resolveAskUserStatus('answered', []), 'answered');
});

test('preserves skipped only when the whole interaction has no user input', () => {
  const emptyAnswers = [
    { question: 'First?', selected_options: [], custom_input: '' },
    { question: 'Second?', selected_options: ['Other'], custom_input: '   ' },
  ];

  assert.equal(resolveAskUserStatus('skipped', emptyAnswers), 'skipped');
  assert.equal(hasAskUserInput(emptyAnswers[1]), false);
});

test('page-level skip keeps the interaction answered when another page has input', () => {
  const partialAnswers = [
    { question: 'First?', selected_options: ['A'], custom_input: '' },
    { question: 'Second?', selected_options: [], custom_input: '' },
  ];

  assert.equal(resolveAskUserStatus('skipped', partialAnswers), 'answered');
});

test('whole-interaction cancellation creates only empty answer shells', () => {
  assert.deepEqual(buildEmptyAskUserAnswers([{ question: 'First?' }, { question: 'Second?' }]), [
    { question: 'First?', selected_options: [], custom_input: '' },
    { question: 'Second?', selected_options: [], custom_input: '' },
  ]);
});

test('failed approval submission unlocks only the same pending request', () => {
  assert.equal(shouldUnlockPendingApproval('approval-1', 'approval-1'), true);
  assert.equal(shouldUnlockPendingApproval(null, 'approval-1'), false);
  assert.equal(shouldUnlockPendingApproval('approval-2', 'approval-1'), false);
});
