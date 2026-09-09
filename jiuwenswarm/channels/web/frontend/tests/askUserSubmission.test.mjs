import assert from 'node:assert/strict';
import test from 'node:test';

import {
  buildEmptyAskUserAnswers,
  hasAskUserInput,
  resolveAskUserStatus,
  shouldUnlockPendingApproval,
} from '../node_modules/.cache/ask-user-submission/components/InteractionSlot/interactionSubmission.js';

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
