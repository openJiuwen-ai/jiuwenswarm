import './fixtures/browserEnvironment.mjs';
import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { A2UIProvider } from '@a2ui/react';

await build({
  entryPoints: ['src/components/ChatPanel/MessageList.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/chat-timeline-list/MessageList.mjs',
  define: { 'import.meta.env': '{}' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
  plugins: [
    {
      name: 'ui-test-svg-components',
      setup(builder) {
        builder.onResolve({ filter: /\.svg\?react$/ }, ({ path }) => ({ path, namespace: 'svg-react-stub' }));
        builder.onLoad({ filter: /.*/, namespace: 'svg-react-stub' }, () => ({
          contents: 'export default function SvgStub() { return null; }',
          loader: 'js',
        }));
      },
    },
  ],
});

const { ChatTimelineList, TurnElapsed } = await import('../node_modules/.cache/chat-timeline-list/MessageList.mjs');

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

test('expert Team timeline summary uses the frozen leader identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
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

test('Expert Team timeline surface uses the selected group identity', () => {
  const markup = renderToStaticMarkup(
    createElement(TurnElapsed, {
      startMs: 1_700_000_000_000,
      endMs: 1_700_000_001_000,
      isLastTurn: false,
      showAvatar: true,
      teamLayout: true,
      teamLeaderIdentity: {
        agentTemplateId: 'leader-template',
        displayName: '专家团负责人',
      },
      teamGroupIdentity: {
        id: 'reply-confirmation',
        displayName: '只会回复好的收到',
        avatarUrl: null,
      },
    }),
  );

  assert.match(markup, /只会回复好的收到/);
  assert.doesNotMatch(markup, /专家团负责人/);
});

test('expert group identity is preserved in bounded history and complete static export', () => {
  const messages = Array.from({ length: 200 }, (_, index) => ({
    id: `history-${index}`,
    role: index % 2 === 0 ? 'user' : 'assistant',
    content: `history-content-${index}-end`,
    timestamp: new Date(1_700_000_000_000 + index * 1_000).toISOString(),
  }));
  for (const virtualized of [true, false]) {
    const markup = renderToStaticMarkup(createElement(A2UIProvider, null, createElement(ChatTimelineList, {
      messages,
      mode: 'team',
      staticTimeline: true,
      virtualized,
      teamGroupIdentityOverride: {
        id: 'history-experts',
        displayName: 'History Expert Group',
        avatarUrl: null,
      },
    })));

    assert.match(markup, /History Expert Group/);
    const renderedMessages = [...markup.matchAll(/history-content-\d+-end/g)];
    if (virtualized) {
      // SSR has no laid-out viewport; bottom scrolling happens after mount.
      assert.ok(renderedMessages.length > 0 && renderedMessages.length < messages.length);
    } else {
      assert.equal(renderedMessages.length, messages.length);
      assert.match(markup, /history-content-0-end/);
      assert.match(markup, /history-content-199-end/);
    }
    assert.equal(markup.includes('data-virtualized="true"'), virtualized);
  }
});
