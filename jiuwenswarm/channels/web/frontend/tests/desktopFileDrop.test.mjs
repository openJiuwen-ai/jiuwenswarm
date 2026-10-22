import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { transformSync } from 'esbuild';
import React, { act, useCallback, useEffect, useLayoutEffect, useRef } from 'react';
import { createRoot } from 'react-dom/client';
import { JSDOM } from 'jsdom';

async function fixture(describeDroppedFiles) {
  const target = new EventTarget();
  target.pywebview = { api: { select_local_files() {} } };
  if (describeDroppedFiles) target.jiuwenDesktop = { describeDroppedFiles };
  globalThis.window = target;
  const picker = await import(`../node_modules/.cache/desktop-file-drop/localFilePicker.mjs?case=${Math.random()}`);
  picker.installDesktopFileDragAccept();
  const received = [];
  const unregister = picker.registerDesktopLocalFilesConsumer((detail, files) => received.push({ detail, files }));
  return { target, picker, received, unregister };
}

function drop(target, { directory = false, virtual = false } = {}) {
  const event = new (target.Event || Event)('drop', { cancelable: true });
  const file = { name: 'bundle.zip' };
  event.dataTransfer = { types: ['Files'], files: [file], items: [{
    kind: 'file', webkitGetAsEntry: () => virtual ? null : { isDirectory: directory, isFile: !directory },
  }] };
  event.clientX = 40;
  event.clientY = 60;
  target.dispatchEvent(event);
  return event;
}

const picks = [{ path: '/tmp/bundle.zip', filename: 'bundle.zip', size: 12, kind: 'document' }];
const flush = () => new Promise(resolve => setImmediate(resolve));

test('native drop delivers normalized picks, coordinates and one stable id to the shared consumer', async () => {
  let requested;
  const f = await fixture(async files => { requested = files; return picks; });
  const events = [];
  f.target.addEventListener(f.picker.DESKTOP_LOCAL_FILES_EVENT, event => events.push(event.detail));
  assert.equal(drop(f.target).defaultPrevented, true);
  await flush();
  assert.equal(requested[0].name, 'bundle.zip');
  assert.equal(f.received.length, 1);
  assert.equal(f.received[0].files[0].path, '/tmp/bundle.zip');
  assert.equal(f.received[0].detail.clientX, 40);
  assert.equal(f.received[0].detail.clientY, 60);
  assert.equal(f.received[0].detail.source, 'drop');
  assert.ok(f.received[0].detail.dropId);
  assert.equal(events[0].dropId, f.received[0].detail.dropId);
});

test('connector consumer receives a later drop after the chat consumer unmounts', async () => {
  const f = await fixture(async () => picks);
  f.unregister();
  const imports = [];
  f.picker.registerDesktopLocalFilesConsumer((_detail, files) => imports.push(files[0].path));
  drop(f.target);
  await flush();
  assert.deepEqual(imports, ['/tmp/bundle.zip']);
  assert.equal(f.received.length, 0);
});

test('a slow drop is not redirected to a newly mounted consumer', async () => {
  let resolve;
  const f = await fixture(() => new Promise(done => { resolve = done; }));
  drop(f.target);
  await flush();
  f.unregister();
  const other = [];
  f.picker.registerDesktopLocalFilesConsumer(detail => other.push(detail));
  resolve(picks);
  await flush();
  assert.equal(other.length, 0);
  assert.equal(f.received.length, 0);
});

test('directories and virtual drags are rejected before requesting native paths', async () => {
  let calls = 0;
  const f = await fixture(async () => { calls++; return picks; });
  const rejected = [];
  for (const name of [f.picker.DESKTOP_DIRECTORY_DROP_REJECTED_EVENT, f.picker.DESKTOP_VIRTUAL_FILE_DROP_REJECTED_EVENT]) {
    f.target.addEventListener(name, () => rejected.push(name));
  }
  drop(f.target, { directory: true });
  drop(f.target, { virtual: true });
  await flush();
  assert.equal(calls, 0);
  assert.equal(rejected.length, 2);
});

test('an empty native path result reports an unsupported virtual file', async () => {
  const f = await fixture(async () => []);
  let rejected = 0;
  f.target.addEventListener(f.picker.DESKTOP_VIRTUAL_FILE_DROP_REJECTED_EVENT, () => rejected++);
  drop(f.target);
  await flush();
  assert.equal(rejected, 1);
  assert.equal(f.received.length, 0);
});

test('older Electron and pywebview shells keep their existing native delivery path', async () => {
  const f = await fixture();
  drop(f.target);
  await flush();
  assert.equal(f.received.length, 0);
  f.target.__JIUWEN_INGEST_LOCAL_FILES__({ source: 'drop', files: picks });
  assert.equal(f.received.length, 1);
});

// Exercise the actual ChatPanel registration effect under React's commit lifecycle.
// Keep the consumer callback stable across session changes, as ChatPanel does.
const chatSource = readFileSync(new URL('../src/components/ChatPanel/index.tsx', import.meta.url), 'utf8');
const registrationMarker = chatSource.indexOf('// Durable bridge lives in localFilePicker;');
const registrationStart = Math.max(
  chatSource.lastIndexOf('  useEffect(() => {', registrationMarker),
  chatSource.lastIndexOf('  useLayoutEffect(() => {', registrationMarker),
);
const registrationEnd = chatSource.indexOf(']);', registrationMarker) + 3;
assert.ok(registrationMarker > 0 && registrationStart >= 0 && registrationEnd > registrationMarker);
const registerChatPanel = new Function(
  'useEffect', 'useLayoutEffect', 'activeSessionId', 'registerDesktopLocalFilesConsumer',
  'ingestDesktopLocalFiles', 'normalizePicks', 'markDesktopFileDropZoneActive',
  'DESKTOP_LOCAL_FILES_EVENT', 'DESKTOP_FILE_DRAG_EVENT',
  transformSync(chatSource.slice(registrationStart, registrationEnd), { loader: 'ts' }).code,
);

for (const sessions of [['A', 'B'], ['A', 'B', 'A'], ['A', 'A']]) {
  test(`mounted ChatPanel scopes pending drops to the session commit: ${sessions.join(' -> ')}`, async () => {
    const previous = {
      window: globalThis.window, document: globalThis.document,
      CustomEvent: globalThis.CustomEvent, actEnvironment: globalThis.IS_REACT_ACT_ENVIRONMENT,
    };
    const dom = new JSDOM('<div id="root"></div>', { url: 'http://localhost:5173' });
    globalThis.window = dom.window;
    globalThis.document = dom.window.document;
    globalThis.CustomEvent = dom.window.CustomEvent;
    globalThis.IS_REACT_ACT_ENVIRONMENT = true;
    const pending = [];
    dom.window.pywebview = { api: { select_local_files() {} } };
    dom.window.jiuwenDesktop = {
      describeDroppedFiles: () => new Promise(resolve => pending.push(resolve)),
    };
    const picker = await import(`../node_modules/.cache/desktop-file-drop/localFilePicker.mjs?react=${sessions.join('-')}`);
    picker.installDesktopFileDragAccept();
    const delivered = [];
    let mounts = 0;
    function MountedChatPanel({ sessionId }) {
      const active = useRef(sessionId);
      const lastDrop = useRef(null);
      useLayoutEffect(() => { active.current = sessionId; }, [sessionId]);
      useEffect(() => { mounts++; }, []);
      const ingest = useCallback((detail, files) => {
        // Model the existing ChatPanel consumer + CustomEvent deduplication.
        if (lastDrop.current === detail.dropId) return;
        lastDrop.current = detail.dropId;
        delivered.push({ sessionId: active.current, path: files[0].path });
      }, []);
      const mark = useCallback(() => {}, []);
      registerChatPanel(useEffect, useLayoutEffect, sessionId, picker.registerDesktopLocalFilesConsumer,
        ingest, picker.normalizePicks, mark, picker.DESKTOP_LOCAL_FILES_EVENT, picker.DESKTOP_FILE_DRAG_EVENT);
      return null;
    }
    const root = createRoot(dom.window.document.getElementById('root'));
    try {
      await act(async () => root.render(React.createElement(MountedChatPanel, { sessionId: sessions[0] })));
      await act(async () => { drop(dom.window); });
      assert.equal(pending.length, 1);
      for (const sessionId of sessions.slice(1)) {
        await act(async () => root.render(React.createElement(MountedChatPanel, { sessionId })));
      }
      await act(async () => pending.shift()(picks));
      assert.equal(mounts, 1, 'The regression must not remount ChatPanel');
      const changedSession = sessions.some(sessionId => sessionId !== sessions[0]);
      assert.deepEqual(delivered, changedSession ? [] : [{ sessionId: 'A', path: '/tmp/bundle.zip' }]);
      delivered.length = 0;
      await act(async () => { drop(dom.window); });
      await act(async () => pending.shift()(picks));
      assert.deepEqual(delivered, [{ sessionId: sessions.at(-1), path: '/tmp/bundle.zip' }]);
    } finally {
      await act(async () => root.unmount());
      dom.window.close();
      globalThis.window = previous.window;
      globalThis.document = previous.document;
      globalThis.CustomEvent = previous.CustomEvent;
      globalThis.IS_REACT_ACT_ENVIRONMENT = previous.actEnvironment;
    }
  });
}
