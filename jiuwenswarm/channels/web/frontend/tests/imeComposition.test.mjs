import assert from 'node:assert/strict';
import test from 'node:test';

import { isImeCompositionKey } from '../node_modules/.cache/ime-composition/components/ChatPanel/imeComposition.js';

const keyboardEvent = (overrides = {}) => ({
  isComposing: false,
  key: 'Enter',
  keyCode: 13,
  ...overrides,
});

test('does not treat a regular Enter as IME composition', () => {
  assert.equal(isImeCompositionKey(keyboardEvent(), false), false);
});

test('detects the component composition state', () => {
  assert.equal(isImeCompositionKey(keyboardEvent(), true), true);
});

test('detects the standard KeyboardEvent composition flag', () => {
  assert.equal(isImeCompositionKey(keyboardEvent({ isComposing: true }), false), true);
});

test('detects engines that expose an IME key as Process', () => {
  assert.equal(isImeCompositionKey(keyboardEvent({ key: 'Process' }), false), true);
});

test('detects Safari and WKWebView IME processing keyCode 229', () => {
  assert.equal(isImeCompositionKey(keyboardEvent({ isComposing: false, key: 'Enter', keyCode: 229 }), false), true);
});
