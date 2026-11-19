import assert from 'node:assert/strict';
import test from 'node:test';

globalThis.window = globalThis;
globalThis.window.location = { protocol: 'http:', host: 'localhost:18180' };
const storageValues = new Map();
globalThis.localStorage = {
  getItem: (key) => storageValues.get(key) ?? null,
  setItem: (key, value) => storageValues.set(key, value),
  removeItem: (key) => storageValues.delete(key),
};

// 与 webClientRuntimeAck.test.mjs 同款假 WebSocket：send 只记录请求，
// 响应由测试用 receive 按请求 id 手动投递，从而精确控制响应到达顺序。
class FakeWebSocket {
  static OPEN = 1;
  static instance;

  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sentRequests = [];
    FakeWebSocket.instance = this;
    queueMicrotask(() => {
      this.readyState = FakeWebSocket.OPEN;
      this.onopen?.();
    });
  }

  addEventListener() {}

  send(raw) {
    this.sentRequests.push(JSON.parse(raw));
  }

  close() {}

  receive(message) {
    this.onmessage?.({ data: JSON.stringify(message) });
  }
}

globalThis.WebSocket = FakeWebSocket;

const { useCronStore } = await import('../node_modules/.cache/cron-store-jobs-race/stores/cronStore.mjs');
const { webClient } = await import('../node_modules/.cache/cron-store-jobs-race/services/webClient.mjs');

await webClient.connect();

const socket = FakeWebSocket.instance;

function jobsPayload(ids) {
  return {
    jobs: ids.map((id) => ({ id, name: `job-${id}`, targets: 'web', project_id: 'default' })),
  };
}

function respond(id, payload) {
  socket.receive({ type: 'res', id, ok: true, payload });
}

function respondError(id, message) {
  socket.receive({ type: 'res', id, ok: false, error: message });
}

// loadJobs 发请求要过若干个 await 点；轮询等到新请求出现在 sentRequests 再返回其 id
async function waitForSend() {
  const before = socket.sentRequests.length;
  for (let i = 0; i < 100 && socket.sentRequests.length === before; i += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
  const sent = socket.sentRequests[socket.sentRequests.length - 1];
  assert.ok(sent, 'request was not sent');
  return sent.id;
}

test('单个请求正常写回列表并复位加载状态', async () => {
  const pending = useCronStore.getState().loadJobs();
  const id = await waitForSend();
  respond(id, jobsPayload(['a', 'b']));
  assert.equal(await pending, true);
  assert.deepEqual(
    useCronStore.getState().jobs.map((j) => j.id),
    ['a', 'b'],
  );
  assert.equal(useCronStore.getState().isLoading, false);
});

test('旧快照晚到不整表覆盖新列表（issue 核心场景）', async () => {
  const first = useCronStore.getState().loadJobs();
  const id1 = await waitForSend();
  const second = useCronStore.getState().loadJobs();
  const id2 = await waitForSend();

  // 新请求先回：列表含刚创建的任务
  respond(id2, jobsPayload(['kept', 'fresh']));
  assert.equal(await second, true);
  assert.deepEqual(
    useCronStore.getState().jobs.map((j) => j.id),
    ['kept', 'fresh'],
  );

  // 旧请求后回：拍的是更早的快照（不含新任务），修复前会整表回退
  respond(id1, jobsPayload(['stale']));
  assert.equal(await first, true);
  assert.deepEqual(
    useCronStore.getState().jobs.map((j) => j.id),
    ['kept', 'fresh'],
  );
  assert.equal(useCronStore.getState().isLoading, false);
});

test('旧请求失败不复位新请求的加载中状态，返回 true 停掉旧重试梯', async () => {
  const first = useCronStore.getState().loadJobs();
  const id1 = await waitForSend();
  const second = useCronStore.getState().loadJobs();
  const id2 = await waitForSend();
  assert.equal(useCronStore.getState().isLoading, true);

  respondError(id1, 'boom');
  assert.equal(await first, true);
  // isLoading 归最新请求管，未被旧失败复位
  assert.equal(useCronStore.getState().isLoading, true);

  respond(id2, jobsPayload(['after-failure']));
  assert.equal(await second, true);
  assert.deepEqual(
    useCronStore.getState().jobs.map((j) => j.id),
    ['after-failure'],
  );
  assert.equal(useCronStore.getState().isLoading, false);
});

test('最新请求失败照旧清加载状态并返回 false 供调用方重试', async () => {
  const pending = useCronStore.getState().loadJobs();
  const id = await waitForSend();
  respondError(id, 'boom');
  assert.equal(await pending, false);
  assert.equal(useCronStore.getState().isLoading, false);
});
