import assert from 'node:assert/strict';
import test from 'node:test';
import {
  JoyAIFrameClock,
  JoyAIPromptLifecycle,
} from '../../../../channels/web/frontend/node_modules/.cache/joyai-prompt-lifecycle/joyaiPromptLifecycle.js';

test('the next frame consumes only the latest queued JoyAI prompt', async () => {
  const lifecycle = new JoyAIPromptLifecycle();
  const first = lifecycle.enqueue('first instruction', 'first question');
  const second = lifecycle.enqueue('latest instruction', 'latest question');

  const claimed = lifecycle.claim();
  assert.ok(claimed);
  assert.equal(claimed.instruction, 'latest instruction');
  assert.equal(claimed.question, 'latest question');
  assert.equal(lifecycle.hasPending, false);

  const result = { response: 'handled latest prompt' };
  claimed.complete(result);
  assert.deepEqual(await first, result);
  assert.deepEqual(await second, result);
});

test('reset settles a prompt that has not reached a frame yet', async () => {
  const lifecycle = new JoyAIPromptLifecycle();
  const pending = lifecycle.enqueue('instruction', 'question');

  lifecycle.reset();

  assert.equal(await pending, null);
  assert.equal(lifecycle.hasPending, false);
});

test('frame timestamps advance by one fixed inference turn', () => {
  const clock = new JoyAIFrameClock(1_000);

  assert.equal(clock.nextRange(), '0.0 seconds ~ 1.0 seconds');
  assert.equal(clock.nextRange(), '1.0 seconds ~ 2.0 seconds');
  assert.equal(clock.nextRange(), '2.0 seconds ~ 3.0 seconds');
  clock.reset();
  assert.equal(clock.nextRange(), '0.0 seconds ~ 1.0 seconds');
});
