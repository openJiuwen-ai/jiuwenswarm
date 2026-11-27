import { describe, expect, it } from 'vitest';

import { prepareUserTurn } from './userTurn';

function createHarness() {
  const callOrder: string[] = [];
  const messages: Array<{
    id: string;
    role: string;
    content: string;
    timestamp: string;
  }> = [];

  return {
    callOrder,
    messages,
    run(content: string) {
      prepareUserTurn(content, {
        bumpUserInputVersion: () => {
          callOrder.push('bump');
        },
        stopAudio: () => {
          callOrder.push('stop');
        },
        clearContextWindowUsage: () => {
          callOrder.push('clear');
        },
        addMessage: (message) => {
          callOrder.push('addMessage');
          messages.push(message);
        },
        now: () => 123456,
        timestamp: () => '2026-04-15T00:00:00.000Z',
      });
    },
  };
}

describe('prepareUserTurn', () => {
  it('sendMessage path clears context window before appending a user message', () => {
    const harness = createHarness();

    harness.run('hello');

    expect(harness.callOrder).toEqual(['bump', 'stop', 'clear', 'addMessage']);
    expect(harness.messages).toEqual([
      {
        id: 'user-123456',
        role: 'user',
        content: 'hello',
        timestamp: '2026-04-15T00:00:00.000Z',
      },
    ]);
  });

  it('supplement path also clears context window before appending the new user message', () => {
    const harness = createHarness();

    harness.run('more details');

    expect(harness.callOrder).toEqual(['bump', 'stop', 'clear', 'addMessage']);
    expect(harness.messages).toEqual([
      {
        id: 'user-123456',
        role: 'user',
        content: 'more details',
        timestamp: '2026-04-15T00:00:00.000Z',
      },
    ]);
  });
});
