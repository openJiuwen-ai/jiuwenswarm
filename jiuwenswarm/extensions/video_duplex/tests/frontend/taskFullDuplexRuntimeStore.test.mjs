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
  stopTaskFullDuplex, useTaskFullDuplexRuntime, setTaskDuplexScreenSharing,
  startTaskDuplexScreenSharing, stopTaskDuplexScreenSharing,
} = await import(`data:text/javascript;base64,${Buffer.from(compiled.outputFiles[0].text).toString('base64')}`);

const snapshot = () => useTaskFullDuplexRuntime();
// TaskFullDuplexAction: the button only shows the run of its own conversation.
const buttonState = (sessionId) => {
  const { state, sessionId: runtimeSessionId } = snapshot();
  return runtimeSessionId === sessionId ? state : 'idle';
};

function registerPanel() {
  const panel = { stops: 0, shares: 0 };
  registerTaskFullDuplexController({
    // The screen was picked; the model connects later, once the picture plays.
    startScreenDuplex: async () => true,
    stop: () => { panel.stops += 1; },
    setMicrophoneMuted: () => {},
    setSpeakerMuted: () => {},
    stopScreenSharing: () => { setTaskDuplexScreenSharing(false); },
    startScreenSharing: async () => { panel.shares += 1; setTaskDuplexScreenSharing(true); },
  }, async (sessionId) => `task-duplex:${sessionId}`, async () => {});
  return panel;
}

test('closing the shared screen keeps the run and it can be shared again', async () => {
  const panel = registerPanel();
  await startTaskFullDuplex('sess-share', 'screen');
  setTaskFullDuplexRuntimeState('starting');
  setTaskFullDuplexRuntimeState('active');
  setTaskDuplexScreenSharing(true);

  stopTaskDuplexScreenSharing();
  assert.equal(snapshot().screenSharing, false);
  assert.equal(buttonState('sess-share'), 'active');

  startTaskDuplexScreenSharing();
  await Promise.resolve();
  assert.equal(panel.shares, 1);
  assert.equal(snapshot().screenSharing, true);
  assert.equal(panel.stops, 0);
  assert.equal(buttonState('sess-share'), 'active');

  // Already sharing, or no active run: nothing to start.
  startTaskDuplexScreenSharing();
  stopTaskFullDuplex();
  setTaskFullDuplexRuntimeState('idle');
  startTaskDuplexScreenSharing();
  assert.equal(panel.shares, 1);
});
