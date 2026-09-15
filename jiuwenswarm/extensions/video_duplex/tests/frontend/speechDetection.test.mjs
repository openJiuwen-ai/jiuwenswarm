import assert from "node:assert/strict";
import test from "node:test";
import { SpeechGate } from "../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/speechGate.mjs";
import { SileroVad } from "../../../../channels/web/frontend/node_modules/.cache/realtime-duplex/sileroVad.mjs";

function feed(gate, probability, frames, level = 1000) {
  return Array.from({ length: frames }, () => gate.process(probability, level));
}

test("loud non-speech and isolated high-confidence bursts do not start a turn", () => {
  const gate = new SpeechGate();
  const events = [
    ...feed(gate, 0.1, 100, 14000),
    ...feed(gate, 0.99, 3, 14000),
    ...feed(gate, 0.1, 12),
    ...feed(gate, 0.99, 3),
  ];
  assert.ok(events.every((event) => event.state !== "started"));
});

test("sustained speech confirms at 256ms and allows two uncertain frames", () => {
  const gate = new SpeechGate();
  assert.equal(feed(gate, 0.95, 8).at(-1).state, "started");
  const interrupted = new SpeechGate();
  const events = [
    ...feed(interrupted, 0.95, 4),
    ...feed(interrupted, 0.2, 2),
    ...feed(interrupted, 0.95, 4),
  ];
  assert.equal(events.at(-1).state, "started");
  assert.equal(events.filter((event) => event.state === "started").length, 1);
});

test("quiet speech after a loud background is not blocked by an inflated volume threshold", () => {
  const gate = new SpeechGate();
  feed(gate, 0.1, 100, 10000);
  assert.equal(feed(gate, 0.95, 8, 120).at(-1).state, "started");
});

test("brief pauses do not re-arm interruption; a completed utterance does", () => {
  const gate = new SpeechGate();
  const first = [
    ...feed(gate, 0.95, 12),
    ...feed(gate, 0.1, 10),
    ...feed(gate, 0.95, 50),
  ];
  assert.equal(first.filter((event) => event.state === "started").length, 1);
  assert.equal(feed(gate, 0.1, 20).at(-1).state, "ended");
  assert.equal(feed(gate, 0.95, 8).at(-1).state, "started");
});

function mockWorker(fakeTimers = false) {
  const original = globalThis.Worker;
  const originalWindow = globalThis.window;
  let worker;
  const timers = new Map();
  let timerSequence = 0;
  globalThis.window = fakeTimers ? {
    setTimeout(callback, delay) { const id = ++timerSequence; timers.set(id, { callback, delay }); return id; },
    clearTimeout(id) { timers.delete(id); },
  } : globalThis;
  globalThis.Worker = class {
    messages = [];
    terminated = false;
    constructor() {
      worker = this;
    }
    postMessage(message) {
      this.messages.push(message);
    }
    terminate() {
      this.terminated = true;
    }
    emit(data) {
      const generation = this.messages.filter((message) => message.type === 'audio').at(-1)?.generation ?? 0;
      this.onmessage({ data: { generation, ...data } });
    }
  };
  return {
    fireTimer(delay) {
      const entry = [...timers].find(([, timer]) => timer.delay === delay);
      if (!entry) return false;
      timers.delete(entry[0]);
      entry[1].callback();
      return true;
    },
    get worker() {
      return worker;
    },
    restore() {
      globalThis.Worker = original;
      globalThis.window = originalWindow;
    },
  };
}

test("worker inference is serialized and stopping discards late results", async () => {
  const mock = mockWorker();
  const detections = [];
  const vad = new SileroVad((event) => detections.push(event), assert.fail);
  try {
    const ready = vad.start();
    mock.worker.emit({ type: "ready" });
    await ready;
    vad.push(new Int16Array(1600));
    vad.push(new Int16Array(1600));
    assert.equal(mock.worker.messages.length, 2); // init + one audio batch
    mock.worker.emit({
      type: "detections",
      detections: [{ state: "idle" }],
      capturedAt: performance.now(),
    });
    assert.equal(mock.worker.messages.length, 3);
    vad.stop();
    mock.worker.emit({
      type: "detections",
      detections: [{ state: "started" }],
      capturedAt: performance.now(),
    });
    assert.equal(detections.length, 1);
    assert.equal(mock.worker.terminated, true);
  } finally {
    vad.stop();
    mock.restore();
  }
});

test("stale inference is discarded and fresh inference recovers without stopping the worker", async () => {
  const mock = mockWorker();
  const errors = [];
  const detections = [];
  const vad = new SileroVad((event) => detections.push(event), (message) => errors.push(message));
  try {
    const ready = vad.start();
    mock.worker.emit({ type: "ready" });
    await ready;
    mock.worker.emit({
      type: "detections",
      detections: [{ state: "started" }],
      capturedAt: performance.now() - 600,
    });
    assert.equal(errors.length, 0);
    assert.equal(detections.length, 0);
    assert.equal(mock.worker.terminated, false);
    vad.push(new Int16Array(1600));
    assert.equal(mock.worker.messages.at(-1).reset, true);
    mock.worker.emit({ type: 'detections', detections: [{ state: 'started' }], capturedAt: performance.now() });
    assert.equal(detections.length, 1);
  } finally {
    vad.stop();
    mock.restore();
  }
});

test('an audio burst keeps the latest chunk and ignores superseded in-flight detections', async () => {
  const mock = mockWorker();
  const detections = [];
  const events = [];
  const vad = new SileroVad((event) => detections.push(event), assert.fail, (event) => events.push(event));
  try {
    const ready = vad.start();
    mock.worker.emit({ type: 'ready' });
    await ready;
    for (let i = 1; i <= 6; i++) vad.push(new Int16Array(1600).fill(i));
    assert.equal(mock.worker.terminated, false);
    assert.equal(mock.worker.messages.length, 2);
    mock.worker.emit({ type: 'detections', detections: [{ state: 'started' }], capturedAt: performance.now(), generation: 0 });
    assert.equal(detections.length, 0);
    const fresh = mock.worker.messages.at(-1);
    assert.equal(new Int16Array(fresh.pcm)[0], 6);
    assert.equal(fresh.reset, true);
    mock.worker.emit({ type: 'detections', detections: [{ state: 'started' }], capturedAt: performance.now() });
    assert.equal(detections.length, 1);
    assert.deepEqual(events, ['qwen_vad_resync', 'qwen_vad_recovered']);
  } finally { vad.stop(); mock.restore(); }
});

test('a hung worker restarts automatically and late messages from the old worker are ignored', async () => {
  const mock = mockWorker(true);
  const detections = [];
  const vad = new SileroVad((event) => detections.push(event), assert.fail);
  try {
    const ready = vad.start();
    mock.worker.emit({ type: 'ready' });
    await ready;
    const oldWorker = mock.worker;
    vad.push(new Int16Array(1600));
    vad.push(new Int16Array(1600));
    assert.equal(mock.fireTimer(2000), true);
    assert.equal(oldWorker.terminated, true);
    assert.equal(mock.fireTimer(0), true);
    assert.notEqual(mock.worker, oldWorker);
    oldWorker.emit({ type: 'detections', detections: [{ state: 'started' }], capturedAt: performance.now() });
    assert.equal(detections.length, 0);
    mock.worker.emit({ type: 'ready' });
    assert.equal(mock.worker.messages.at(-1).type, 'audio');
    mock.worker.emit({ type: 'detections', detections: [{ state: 'started' }], capturedAt: performance.now() });
    assert.equal(detections.length, 1);
  } finally { vad.stop(); mock.restore(); }
});

test('stopping a session cancels an automatic worker restart', async () => {
  const mock = mockWorker(true);
  const vad = new SileroVad(assert.fail, assert.fail);
  try {
    const ready = vad.start();
    mock.worker.emit({ type: 'ready' });
    await ready;
    mock.worker.emit({ type: 'error', message: 'runtime failure' });
    vad.stop();
    assert.equal(mock.fireTimer(0), false);
  } finally { vad.stop(); mock.restore(); }
});

test("startup errors reject and stop the worker without enabling volume-only fallback", async () => {
  const mock = mockWorker();
  const vad = new SileroVad(assert.fail, assert.fail);
  try {
    const ready = vad.start();
    mock.worker.emit({ type: "error", message: "model unavailable" });
    await assert.rejects(ready, /model unavailable/);
    assert.equal(mock.worker.terminated, true);
  } finally {
    vad.stop();
    mock.restore();
  }
});
