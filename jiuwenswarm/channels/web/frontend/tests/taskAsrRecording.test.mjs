import assert from 'node:assert/strict';
import { afterEach, beforeEach, test } from 'node:test';
import { resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { build } from 'esbuild';
import { act, createElement } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

const outfile = resolve('node_modules/.cache/task-asr-recording/hook.mjs');
await build({
  entryPoints: ['src/features/taskAsr/useTaskAsr.ts'],
  outfile,
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  plugins: [
    {
      name: 'asr-network',
      setup(builder) {
        builder.onResolve({ filter: /services\/webClient$/ }, () => ({ path: 'network', namespace: 'asr' }));
        builder.onLoad({ filter: /.*/, namespace: 'asr' }, () => ({
          contents: 'export const webRequest = (...args) => globalThis.__asrTest.request(...args);',
        }));
      },
    },
  ],
});
const { useTaskAsr } = await import(pathToFileURL(outfile));
let dom, root, state, stopped, closed, cancelled, requests, errors;
const originalGlobals = new Map();

beforeEach(async () => {
  stopped = closed = cancelled = 0;
  requests = [];
  errors = [];
  dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost/' });
  const stream = {
    getTracks: () => [
      {
        stop() {
          stopped += 1;
        },
      },
    ],
  };
  Object.defineProperty(dom.window.navigator, 'mediaDevices', {
    value: { getUserMedia: async () => stream },
  });
  dom.window.requestAnimationFrame = () => 1;
  dom.window.cancelAnimationFrame = () => {
    cancelled += 1;
  };
  class Recorder {
    static isTypeSupported() {
      return true;
    }
    mimeType = 'audio/webm';
    state = 'inactive';
    start() {
      this.state = 'recording';
    }
    stop() {
      this.state = 'inactive';
      this.ondataavailable?.({ data: new dom.window.Blob(['audio']) });
      this.onstop?.();
    }
  }
  class AudioContext {
    createAnalyser() {
      return {
        fftSize: 256,
        getByteTimeDomainData(data) {
          data.fill(150);
        },
      };
    }
    createMediaStreamSource() {
      return { connect() {} };
    }
    resume() {
      return Promise.resolve();
    }
    close() {
      closed += 1;
      return Promise.resolve();
    }
  }
  for (const [name, value] of Object.entries({
    window: dom.window,
    document: dom.window.document,
    navigator: dom.window.navigator,
    MediaRecorder: Recorder,
    AudioContext,
    Blob: dom.window.Blob,
    FileReader: dom.window.FileReader,
    IS_REACT_ACT_ENVIRONMENT: true,
    __asrTest: {
      request: async (...args) => {
        requests.push(args);
        return { text: 'hello' };
      },
    },
  })) {
    originalGlobals.set(name, Object.getOwnPropertyDescriptor(globalThis, name));
    Object.defineProperty(globalThis, name, { configurable: true, writable: true, value });
  }
  function Harness() {
    state = useTaskAsr({ onTranscript: () => {}, onError: (error) => errors.push(error) });
    return null;
  }
  root = createRoot(document.getElementById('root'));
  await act(async () => root.render(createElement(Harness)));
});

afterEach(async () => {
  await act(async () => root.unmount());
  dom.window.close();
  for (const [name, descriptor] of originalGlobals) {
    if (descriptor) Object.defineProperty(globalThis, name, descriptor);
    else delete globalThis[name];
  }
  originalGlobals.clear();
});

test('recording measures microphone input; stopping releases media and submits one clip', async () => {
  await act(async () => state.toggleRecording());
  assert.equal(state.isRecording, true);
  assert.ok(state.audioLevel > 0);
  await act(async () => {
    state.toggleRecording();
    await new Promise((done) => setTimeout(done, 20));
  });
  assert.equal(state.isRecording, false);
  assert.equal(state.isTranscribing, false);
  assert.equal(state.audioLevel, 0);
  assert.equal(stopped, 1);
  assert.equal(closed, 1);
  assert.equal(cancelled, 1);
  assert.equal(requests.length, 1);
  assert.equal(requests[0][0], 'task.asr.transcribe');
  assert.deepEqual(errors, []);
});

test('leaving during recording discards the clip and closes microphone and audio meter', async () => {
  await act(async () => state.toggleRecording());
  await act(async () => root.render(null));
  assert.equal(requests.length, 0);
  assert.equal(stopped, 1);
  assert.equal(closed, 1);
  assert.equal(cancelled, 1);
});
