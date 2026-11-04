// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.
import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { GitIdentityOption } from '../node_modules/.cache/execution-settings/GitIdentityOption.mjs';

const props = {
  visible: true, checked: false, pending: false, error: false,
  label: '独立 Git 身份', hint: 'Member identity', savingLabel: 'Saving', errorLabel: 'Retry',
  onChange: () => {},
};
const render = (overrides = {}) => renderToStaticMarkup(React.createElement(GitIdentityOption, { ...props, ...overrides }));

test('hidden outside code-team mode', () => assert.equal(render({ visible: false }), ''));
test('renders an accessible unchecked switch by default', () => {
  const html = render();
  assert.match(html, /chat-panel-input-attach-menu-git-identity/);
  assert.match(html, /role="switch"/);
  assert.match(html, /aria-checked="false"/);
  assert.match(html, /独立 Git 身份/);
});
test('shows the acknowledged enabled state and disables while saving', () => {
  const html = render({ checked: true, pending: true });
  assert.match(html, /aria-checked="true"/);
  assert.match(html, /disabled=""/);
  assert.match(html, /aria-label="Saving"/);
});
test('failed saves have a visible alert', () => assert.match(render({ error: true }), /role="alert"[^>]*>Retry/));
