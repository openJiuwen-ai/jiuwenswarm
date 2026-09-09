import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { TurnElapsed } from '../node_modules/.cache/chat-timeline-list/MessageList.mjs';

test('in-progress timeline summary uses the selected Agent identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      agentTemplateName: 'expert-a',
      teamLayout: false,
    }),
  );

  assert.equal(markup.includes('team_leader avatar'), false);
  assert.match(markup, /chat-panel-agent-avatar-name/);
  assert.match(markup, />expert-a<\/span>/);
});

test('Team timeline summary keeps the existing leader identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      agentTemplateName: 'expert-a',
      teamLayout: true,
    }),
  );

  assert.match(markup, /team_leader avatar/);
  assert.equal(markup.includes('chat-panel-agent-avatar-name'), false);
});
