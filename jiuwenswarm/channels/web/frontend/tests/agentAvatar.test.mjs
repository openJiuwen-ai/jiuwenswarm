import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { AgentAvatar } from '../node_modules/.cache/agent-avatar/AgentAvatar.mjs';

test('selected Agent avatar can render its identity name', () => {
  const markup = renderToStaticMarkup(
    createElement(AgentAvatar, {
      agentId: 'expert-a',
      alt: '',
      showName: true,
    }),
  );

  assert.match(markup, /chat-avatar-name/);
  assert.match(markup, />expert-a<\/span>/);
});

test('Team leader override keeps the frozen display name and fallback avatar path', () => {
  const markup = renderToStaticMarkup(
    createElement(AgentAvatar, {
      identityOverride: {
        agentTemplateId: 'leader-template',
        displayName: '专家团负责人',
      },
      alt: '',
      showName: true,
    }),
  );

  assert.match(markup, /chat-avatar-name/);
  assert.match(markup, />专家团负责人<\/span>/);
});
