import assert from 'node:assert/strict';
import { afterEach, test } from 'node:test';

import {
  beginMcpAppMessageRequest,
  dispatchMcpAppMessage,
  getMcpAppContexts,
  normalizeMcpAppMessage,
  registerMcpAppInstance,
  resetMcpAppContextForTests,
  setMcpAppMessageHandler,
  updateMcpAppContext,
} from '../node_modules/.cache/mcp-app-context/mcpAppContext.mjs';

const first = {
  sessionId: 'session-1',
  appInstanceId: 'tool-call-1',
  server: 'budget',
  tool: 'show_budget',
  resourceUri: 'ui://budget/app.html',
};

afterEach(() => resetMcpAppContextForTests());

test('keeps only the latest context for each mounted app instance', () => {
  registerMcpAppInstance(first, 1000);
  updateMcpAppContext(first, { content: [{ type: 'text', text: 'budget: 10' }] }, 1001);
  updateMcpAppContext(first, { structuredContent: { budget: 20 } }, 1002);

  assert.deepEqual(getMcpAppContexts('session-1'), [
    {
      app_instance_id: 'tool-call-1',
      server: 'budget',
      tool: 'show_budget',
      resource_uri: 'ui://budget/app.html',
      structured_content: { budget: 20 },
    },
  ]);
});

test('isolates sessions and clears context when the view unmounts', () => {
  const disposeFirst = registerMcpAppInstance(first, 1000);
  const second = { ...first, sessionId: 'session-2', appInstanceId: 'tool-call-2' };
  registerMcpAppInstance(second, 1000);
  updateMcpAppContext(first, { content: [{ type: 'text', text: 'first' }] }, 1001);
  updateMcpAppContext(second, { content: [{ type: 'text', text: 'second' }] }, 1001);

  assert.equal(getMcpAppContexts('session-1').length, 1);
  assert.equal(getMcpAppContexts('session-2').length, 1);
  disposeFirst();
  assert.deepEqual(getMcpAppContexts('session-1'), []);
});

test('accepts only bounded text messages', () => {
  assert.equal(
    normalizeMcpAppMessage({
      role: 'user',
      content: [
        { type: 'text', text: 'continue' },
        { type: 'text', text: 'with these values' },
      ],
    }).value,
    'continue\n\nwith these values',
  );
  assert.match(
    normalizeMcpAppMessage({ role: 'user', content: [{ type: 'image', data: 'x', mimeType: 'image/png' }] }).error,
    /Only text/,
  );
  assert.match(
    normalizeMcpAppMessage({ role: 'user', content: [{ type: 'text', text: 'x'.repeat(8193) }] }).error,
    /exceeds/,
  );
});

test('enforces update and message request rates without losing the last valid context', () => {
  registerMcpAppInstance(first, 1000);
  for (let index = 0; index < 40; index += 1) {
    assert.equal(
      updateMcpAppContext(
        first,
        {
          content: [{ type: 'text', text: `value ${index}` }],
        },
        1000,
      ).error,
      undefined,
    );
  }
  assert.match(
    updateMcpAppContext(
      first,
      {
        content: [{ type: 'text', text: 'rejected' }],
      },
      1000,
    ).error,
    /quickly/,
  );
  assert.equal(getMcpAppContexts('session-1')[0].content[0].text, 'value 39');

  for (let index = 0; index < 5; index += 1) {
    const pending = beginMcpAppMessageRequest(first, 2000 + index);
    assert.ok(pending.value);
    pending.value();
  }
  assert.match(beginMcpAppMessageRequest(first, 3000).error, /Too many/);
});

test('dispatches an approved app message through the registered chat sender', async () => {
  const sent = [];
  setMcpAppMessageHandler(async (sessionId, content) => {
    sent.push({ sessionId, content });
    return true;
  });

  assert.equal(await dispatchMcpAppMessage('session-1', 'continue'), true);
  assert.deepEqual(sent, [{ sessionId: 'session-1', content: 'continue' }]);
});

test('same UI resource keeps distinct instance state and accepts explicit clearing', () => {
  const second = { ...first, appInstanceId: 'tool-call-2' };
  registerMcpAppInstance(first, 1000);
  registerMcpAppInstance(second, 1000);
  updateMcpAppContext(first, { structuredContent: { place: 'Paris' } }, 1001);
  updateMcpAppContext(second, { structuredContent: { place: 'London' } }, 1002);
  assert.deepEqual(getMcpAppContexts('session-1').map((entry) => entry.structured_content.place), ['Paris', 'London']);
  assert.equal(updateMcpAppContext(first, { content: [], structuredContent: {} }, 1003).value, null);
  assert.deepEqual(getMcpAppContexts('session-1').map((entry) => entry.app_instance_id), ['tool-call-2']);
});

test('rejects excessive context size and depth without replacing accepted state', () => {
  registerMcpAppInstance(first, 1000);
  updateMcpAppContext(first, { structuredContent: { value: 1 } }, 1001);
  assert.match(updateMcpAppContext(first, { structuredContent: { value: 'x'.repeat(32768) } }, 1002).error, /exceeds/);
  let nested = {};
  for (let index = 0; index < 18; index += 1) nested = { child: nested };
  assert.match(updateMcpAppContext(first, { structuredContent: nested }, 1003).error, /depth/);
  assert.deepEqual(getMcpAppContexts('session-1')[0].structured_content, { value: 1 });
});

test('bounds total session context and pending requests and rejects disposed views', () => {
  const dispose = registerMcpAppInstance(first, 1000);
  const second = { ...first, appInstanceId: 'tool-call-2' };
  const third = { ...first, appInstanceId: 'tool-call-3' };
  registerMcpAppInstance(second, 1000);
  registerMcpAppInstance(third, 1000);
  assert.equal(updateMcpAppContext(first, { structuredContent: { value: 'x'.repeat(30000) } }, 1001).error, undefined);
  assert.equal(updateMcpAppContext(second, { structuredContent: { value: 'x'.repeat(30000) } }, 1001).error, undefined);
  assert.match(updateMcpAppContext(third, { structuredContent: { value: 'x'.repeat(10000) } }, 1001).error, /Session/);
  const pending = beginMcpAppMessageRequest(first, 1001);
  assert.ok(pending.value);
  assert.match(beginMcpAppMessageRequest(first, 1002).error, /awaiting/);
  dispose();
  assert.match(beginMcpAppMessageRequest(first, 1003).error, /no longer active/);
  assert.match(updateMcpAppContext(first, { structuredContent: { value: 2 } }, 1003).error, /no longer active/);
  pending.value();
});
