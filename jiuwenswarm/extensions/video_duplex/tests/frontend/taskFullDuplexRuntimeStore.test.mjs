import assert from 'node:assert/strict';
import test from 'node:test';
import { createRequire } from 'node:module';
import { fileURLToPath } from 'node:url';

const require = createRequire(new URL('../../../../channels/web/frontend/package.json', import.meta.url));
const { build } = require('esbuild');
const runtimePath = fileURLToPath(new URL('../../frontend/taskFullDuplexRuntimeStore.ts', import.meta.url));
const compiled = await build({
  stdin: { contents: `export * from ${JSON.stringify(runtimePath)};`,
    resolveDir: fileURLToPath(new URL('../../../../channels/web/frontend/', import.meta.url)) },
  bundle: true, platform: 'node', format: 'esm', write: false,
  // Outside React the hook simply reads the store's current snapshot.
  plugins: [{ name: 'react-stub', setup(b) {
    b.onResolve({ filter: /^react$/ }, () => ({ path: 'react', namespace: 'stub' }));
    b.onLoad({ filter: /.*/, namespace: 'stub' }, () => ({
      contents: 'export const useSyncExternalStore = (_subscribe, get) => get();' }));
  } }],
});

// Screen sharing is switched on for task duplex.
globalThis.window = { localStorage: { getItem: () => 'true', setItem: () => {} } };

const {
  registerTaskFullDuplexController, setTaskFullDuplexRuntimeState, startTaskFullDuplex,
  stopTaskFullDuplex, useTaskFullDuplexRuntime,
} = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);

const snapshot = () => useTaskFullDuplexRuntime();
// TaskFullDuplexAction: the button only shows the run of its own conversation.
const buttonState = (sessionId) => {
  const { state, sessionId: runtimeSessionId } = snapshot();
  return runtimeSessionId === sessionId ? state : 'idle';
};

function registerPanel() {
  const panel = { stops: 0 };
  registerTaskFullDuplexController({
    // The screen was picked; the model connects later, once the picture plays.
    startScreenDuplex: async () => true,
    stop: () => { panel.stops += 1; },
    setMicrophoneMuted: () => {},
    setSpeakerMuted: () => {},
    stopScreenSharing: () => {},
  }, async (sessionId) => `task-duplex:${sessionId}`, async () => {});
  return panel;
}

test('re-renders while the shared screen starts do not drop the run or its conversation (#5825)', async () => {
  registerPanel();
  await startTaskFullDuplex('sess-1', 'screen');
  assert.equal(buttonState('sess-1'), 'starting');

  // The panel re-sends "idle" on every parent render while it waits for the picture.
  setTaskFullDuplexRuntimeState('idle');
  setTaskFullDuplexRuntimeState('idle');
  assert.equal(snapshot().sessionId, 'sess-1');

  setTaskFullDuplexRuntimeState('starting');
  setTaskFullDuplexRuntimeState('active');
  assert.equal(snapshot().sessionId, 'sess-1');
  assert.equal(buttonState('sess-1'), 'active');

  // A real end of the media run still returns the button to idle.
  setTaskFullDuplexRuntimeState('idle');
  assert.equal(snapshot().state, 'idle');
  assert.equal(snapshot().sessionId, null);
});

test('the old run ending after a switch does not clear the new conversation', async () => {
  registerPanel();
  await startTaskFullDuplex('sess-a', 'screen');
  setTaskFullDuplexRuntimeState('starting');
  setTaskFullDuplexRuntimeState('active');
  assert.equal(buttonState('sess-a'), 'active');

  await startTaskFullDuplex('sess-b', 'screen');
  // The stopped panel reports idle only after the new start began.
  setTaskFullDuplexRuntimeState('idle');
  assert.equal(snapshot().sessionId, 'sess-b');
  setTaskFullDuplexRuntimeState('starting');
  setTaskFullDuplexRuntimeState('active');
  assert.equal(buttonState('sess-b'), 'active');

  stopTaskFullDuplex();
  setTaskFullDuplexRuntimeState('idle');
  assert.equal(snapshot().state, 'idle');
});
