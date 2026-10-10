import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import {
  enterJiuwenMateMode,
  exitJiuwenMateMode,
} from '../node_modules/.cache/jiuwenmate-mode/modeState.js';
import { JiuwenMateModeSwitch } from '../node_modules/.cache/jiuwenmate-mode/components/JiuwenMateModeSwitch.js';

test('entering and exiting JiuwenMate preserves the active Swarm navigation page', () => {
  const initial = { isJiuwenMate: false, activeNav: 'skills' };

  const entered = enterJiuwenMateMode(initial);
  assert.deepEqual(entered, { isJiuwenMate: true, activeNav: 'skills' });

  const exited = exitJiuwenMateMode(entered);
  assert.deepEqual(exited, initial);
});

test('the mode switch exposes a stable JiuwenMate entry at the bottom rail', () => {
  const markup = renderToStaticMarkup(
    React.createElement(JiuwenMateModeSwitch, {
      active: false,
      label: 'JiuwenMate',
      onToggle() {},
    }),
  );

  assert.match(markup, /data-testid="session-sidebar-jiuwenmate-switch"/);
  assert.match(markup, /aria-pressed="false"/);
  assert.match(markup, />JiuwenMate</);
});
