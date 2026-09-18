import assert from 'node:assert/strict';
import test from 'node:test';

import {
  toast,
  toastStore,
  TOAST_EXIT_ANIMATION_MS,
} from '../node_modules/.cache/ui-toast/components/ui/Toast/toastStore.js';

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
/** 等到退出动画定时器之后，确保移除回调已执行。 */
const waitForExit = () => sleep(TOAST_EXIT_ANIMATION_MS + 80);

test('close 先标记 closing（动画期间仍渲染），动画结束后移除并触发一次 onClose', async () => {
  const onCloseKeys = [];
  const key = toast.open({ content: 'x', onClose: (k) => onCloseKeys.push(k) });
  assert.equal(toastStore.getSnapshot().length, 1);

  toast.close(key);
  const closingRecord = toastStore.getSnapshot()[0];
  assert.equal(closingRecord.closing, true);

  await waitForExit();
  assert.equal(toastStore.getSnapshot().length, 0);
  assert.deepEqual(onCloseKeys, [key]);
});

test('重复 close 同一个 key 只触发一次 onClose', async () => {
  let closeCount = 0;
  const key = toast.open({ content: 'x', onClose: () => { closeCount += 1; } });

  toast.close(key);
  toast.close(key);
  toast.close(key);
  await waitForExit();

  assert.equal(closeCount, 1);
  assert.equal(toastStore.getSnapshot().length, 0);
});

test('open 透传 variant，closeAll 等动画结束后统一移除且各触发一次 onClose', async () => {
  const onCloseKeys = [];
  const keyA = toast.open({ content: 'a', variant: 'error', onClose: (k) => onCloseKeys.push(k) });
  const keyB = toast.open({ content: 'b', onClose: (k) => onCloseKeys.push(k) });

  const snapshot = toastStore.getSnapshot();
  assert.equal(snapshot.length, 2);
  assert.equal(snapshot[0].variant, 'error');
  assert.equal(snapshot[0].closing, false);
  assert.equal(snapshot[1].variant, 'default');

  toast.closeAll();
  assert.ok(toastStore.getSnapshot().every((record) => record.closing === true));

  await waitForExit();
  assert.equal(toastStore.getSnapshot().length, 0);
  assert.equal(onCloseKeys.length, 2);
  assert.ok(onCloseKeys.includes(keyA));
  assert.ok(onCloseKeys.includes(keyB));
});

test('open 透传 success/warning 变体', async () => {
  const keyS = toast.open({ content: 's', variant: 'success' });
  const keyW = toast.open({ content: 'w', variant: 'warning' });

  const snapshot = toastStore.getSnapshot();
  assert.equal(snapshot.length, 2);
  assert.equal(snapshot[0].key, keyS);
  assert.equal(snapshot[0].variant, 'success');
  assert.equal(snapshot[1].key, keyW);
  assert.equal(snapshot[1].variant, 'warning');

  toast.closeAll();
  await waitForExit();
  assert.equal(toastStore.getSnapshot().length, 0);
});
