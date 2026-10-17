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

test('open 携带同 id 时原地更新并复用原 key；closing 中的条目不复活', async () => {
  const key1 = toast.open({ id: 'conn', content: 'a' });
  const key2 = toast.open({ id: 'conn', content: 'b', variant: 'success' });

  assert.equal(key2, key1);
  const snapshot = toastStore.getSnapshot();
  assert.equal(snapshot.length, 1);
  assert.equal(snapshot[0].content, 'b');
  assert.equal(snapshot[0].variant, 'success');
  assert.equal(snapshot[0].closing, false);

  toast.close(key1);
  const key3 = toast.open({ id: 'conn', content: 'c' });
  assert.notEqual(key3, key1);
  const afterClose = toastStore.getSnapshot();
  assert.equal(afterClose.length, 2);
  assert.equal(afterClose.find((record) => record.key === key1).closing, true);
  assert.equal(afterClose.find((record) => record.key === key3).content, 'c');

  toast.close(key3);
  await waitForExit();
  assert.equal(toastStore.getSnapshot().length, 0);
});

test('同 id 原地更新递增 updatedAt（ToastItem 依据它复位计时器）', async () => {
  const key1 = toast.open({ id: 'progress', content: 'a', duration: 5 });
  const before = toastStore.getSnapshot().find((record) => record.key === key1).updatedAt;

  const key2 = toast.open({ id: 'progress', content: 'b', duration: 5 });
  const after = toastStore.getSnapshot().find((record) => record.key === key2).updatedAt;

  assert.equal(key2, key1);
  assert.ok(after > before, 'updatedAt 必须单调递增');

  toast.closeAll();
  await waitForExit();
  assert.equal(toastStore.getSnapshot().length, 0);
});

test('closeAll 不波及 closable:false 的常驻条目；显式 close 仍可关闭', async () => {
  const persistentKey = toast.open({ id: 'app-connection-toast-message', content: '连接已断开', duration: 0, closable: false });
  const transientKey = toast.open({ content: '归档成功' });

  toast.closeAll();

  const afterCloseAll = toastStore.getSnapshot();
  assert.equal(afterCloseAll.length, 2);
  assert.equal(afterCloseAll.find((record) => record.key === persistentKey).closing, false, '常驻条目不应被 closeAll 关闭');
  assert.equal(afterCloseAll.find((record) => record.key === transientKey).closing, true);

  await waitForExit();
  const afterExit = toastStore.getSnapshot();
  assert.equal(afterExit.length, 1);
  assert.equal(afterExit[0].key, persistentKey);

  toast.close(persistentKey);
  await waitForExit();
  assert.equal(toastStore.getSnapshot().length, 0);
});
