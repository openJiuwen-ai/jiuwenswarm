import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import { ToolGroupDisplay } from '../node_modules/.cache/tool-group-avatar/toolGroupAvatar.mjs';

test('tool group does not fall back to team leader when the turn has an Agent identity', () => {
  const markup = renderToStaticMarkup(
    createElement(ToolGroupDisplay, {
      executions: [
        {
          toolCallId: 'tool-1',
          toolCall: { id: 'tool-1', name: 'bash', arguments: {} },
          status: 'pending',
          startedAt: new Date(1_700_000_000_000).toISOString(),
          updatedAt: new Date(1_700_000_000_000).toISOString(),
        },
      ],
      agentTemplateName: 'expert-a',
    }),
  );

  assert.equal(markup.includes('team_leader avatar'), false);
});

test('team tool groups keep the team leader avatar', () => {
  const markup = renderToStaticMarkup(
    createElement(ToolGroupDisplay, {
      executions: [
        {
          toolCallId: 'tool-2',
          toolCall: { id: 'tool-2', name: 'bash', arguments: {} },
          status: 'pending',
          startedAt: new Date(1_700_000_000_000).toISOString(),
          updatedAt: new Date(1_700_000_000_000).toISOString(),
        },
      ],
      agentTemplateName: 'expert-a',
      teamLayout: true,
    }),
  );

  assert.equal(markup.includes('team_leader avatar'), true);
});

test('expert Team tool groups use the frozen leader identity override', () => {
  const markup = renderToStaticMarkup(
    createElement(ToolGroupDisplay, {
      executions: [
        {
          toolCallId: 'tool-3',
          toolCall: { id: 'tool-3', name: 'bash', arguments: {} },
          status: 'pending',
          startedAt: new Date(1_700_000_000_000).toISOString(),
          updatedAt: new Date(1_700_000_000_000).toISOString(),
        },
      ],
      teamLayout: true,
      teamLeaderIdentity: {
        agentTemplateId: 'leader-template',
        displayName: '专家团负责人',
      },
    }),
  );

  assert.match(markup, /专家团负责人/);
  assert.equal(markup.includes('team_leader avatar'), false);
});
