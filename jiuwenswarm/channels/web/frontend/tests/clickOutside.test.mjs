import assert from 'node:assert/strict';
import test from 'node:test';

import { isClickOutside } from '../node_modules/.cache/click-outside/components/SessionSidebar/clickOutside.js';

const target = {};
const containingBoundary = { contains: candidate => candidate === target };
const outsideBoundary = { contains: () => false };

test('an interaction inside the popover does not close it', () => {
  assert.equal(isClickOutside(target, [containingBoundary, outsideBoundary]), false);
});

test('an interaction on the trigger does not close the popover before its toggle handler runs', () => {
  assert.equal(isClickOutside(target, [outsideBoundary, containingBoundary]), false);
});

test('an interaction outside both the popover and trigger closes it', () => {
  assert.equal(isClickOutside(target, [outsideBoundary, outsideBoundary]), true);
});

test('missing targets or boundaries do not close a mounting popover', () => {
  assert.equal(isClickOutside(null, [outsideBoundary, outsideBoundary]), false);
  assert.equal(isClickOutside(target, [null, outsideBoundary]), false);
});
