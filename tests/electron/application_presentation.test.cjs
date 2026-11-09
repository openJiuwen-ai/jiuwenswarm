const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createApplicationPresentation, clampBounds } = require('../../jiuwenswarm/channels/desktop/electron/application_presentation.cjs');
const { createWindowTransitions, transitionBounds } = require('../../jiuwenswarm/channels/desktop/electron/application_transition.cjs');

function fixture(readyTimeoutMs = 1000, ownerUrl = 'http://127.0.0.1:5173/chat/session/example?dt=secret') {
  const handlers = new Map();
  const windows = [];
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.visible = false;
      this.destroyed = false;
      this.bounds = { x: 100, y: 100, width: 1200, height: 800, ...options };
      this.messages = [];
      this.webContents = Object.assign(new EventEmitter(), {
        mainFrame: {}, getURL: () => ownerUrl,
        send: (channel, value) => this.messages.push({ channel, value }),
        setWindowOpenHandler: handler => { this.openHandler = handler; },
        capturePage: async () => ({ isEmpty: () => false, toDataURL: () => 'data:image/png;base64,c2NyZWVuc2hvdA==' }),
        executeJavaScript: async () => {},
        beginFrameSubscription: (_dirty, callback) => { this.presented = callback; },
        endFrameSubscription: () => { this.presented = null; },
        invalidate: () => queueMicrotask(() => this.presented?.({})),
      });
      windows.push(this);
    }
    isDestroyed() { return this.destroyed; }
    getBounds() { return this.bounds; }
    getContentBounds() { return this.bounds; }
    setBounds(bounds) { this.bounds = bounds; }
    setContentBounds(bounds) { this.bounds = bounds; }
    setOpacity(value) { this.opacity = value; }
    setIgnoreMouseEvents(ignore, options) { this.mouse = { ignore, options }; }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    focus() {}
    moveTop() {}
    isMinimized() { return false; }
    isFullScreen() { return !!this.fullscreen; }
    isMaximized() { return false; }
    maximize() {}
    setFullScreen(enabled) { this.fullscreen = enabled; queueMicrotask(() => this.emit(enabled ? 'enter-full-screen' : 'leave-full-screen')); }
    showInactive() { this.show(); }
    isVisibleOnAllWorkspaces() { return !!this.allWorkspaces; }
    setVisibleOnAllWorkspaces(value) { this.allWorkspaces = value; }
    async loadURL(url) { this.url = url; }
    destroy() { this.destroyed = true; this.emit('closed'); }
  }
  const owner = new Window({});
  owner.webContents.capturePage = async () => ({ isEmpty: () => false, toDataURL: () => 'data:image/png;base64,c2NyZWVuc2hvdA==' });
  owner.show();
  const screen = { getDisplayMatching: () => ({ workArea: { x: 0, y: 0, width: 1440, height: 900 } }) };
  const controller = createApplicationPresentation({
    BrowserWindow: Window,
    ipcMain: { handle: (channel, handler) => handlers.set(channel, handler), removeHandler: channel => handlers.delete(channel) },
    screen,
    getMainWindow: () => owner, preload: '/test/presentation_preload.cjs', readyTimeoutMs,
  });
  const invoke = (name, sender, ...args) => handlers.get('desktop:presentation:' + name)({
    sender: sender.webContents, senderFrame: sender.webContents.mainFrame,
  }, ...args);
  const config = { key: 'instance', title: 'Bee', src: '/extension-assets/bee/g1/index.html',
    resourceUrl: 'http://127.0.0.1:5173/extension-assets/bee/g1/index.html', width: 360, height: 440 };
  return { controller, owner, windows, invoke, config, handlers, screen };
}

test('the workspace stays visible until the plugin confirms readiness; closing restores it', async () => {
  const f = fixture();
  const pending = f.invoke('open', f.owner, f.config);
  const floating = f.windows[1];
  assert.equal(f.owner.visible, true);
  assert.equal(floating.visible, false);
  assert.equal(new URL(floating.url).search, '?application-presentation=1');
  assert.equal(new URL(floating.url).pathname, '/');
  assert.equal(floating.options.transparent, true);
  assert.equal(floating.options.webPreferences.nodeIntegration, false);
  assert.deepEqual(floating.mouse, { ignore: true, options: { forward: true } });
  assert.deepEqual(floating.openHandler(), { action: 'deny' });
  assert.deepEqual((await f.invoke('bootstrap', floating)).initialWorkArea, { x: 0, y: 0, width: 1440, height: 900 });
  f.invoke('ready', floating);
  const { generation } = await pending;
  assert.equal(f.owner.visible, false);
  assert.equal(floating.visible, true);
  f.invoke('hit-test', f.owner, 'instance', true);
  assert.equal(floating.mouse.ignore, false);
  assert.equal(f.invoke('respond', f.owner, 'stale-generation', {}), false);
  assert.equal(f.invoke('respond', f.owner, generation, { id: 'a', ok: true }), true);
  floating.destroy();
  assert.equal(f.owner.visible, true);
  f.controller.dispose();
  assert.equal(f.handlers.size, 0);
});

test('IPC rejects foreign renderers, child frames, and floating-window access to owner operations', async () => {
  const f = fixture();
  const pending = f.invoke('open', f.owner, f.config);
  const floating = f.windows[1];
  assert.throws(() => f.invoke('update', floating, 'instance', {}), /Untrusted/);
  assert.throws(() => f.handlers.get('desktop:presentation:ready')({
    sender: floating.webContents, senderFrame: {},
  }), /Untrusted/);
  f.invoke('ready', floating);
  await pending;
  assert.throws(() => f.invoke('resize', f.owner, 'instance', 960.5, 300), /Invalid/);
  assert.throws(() => f.invoke('move', f.owner, 'instance', Infinity, 0), /Invalid/);
  f.controller.dispose();
});

test('a frontend-only desktop opens the bundled entry file with its presentation query', async () => {
  const f = fixture(1000, 'file:///test/dist/index.html');
  const pending = f.invoke('open', f.owner, f.config);
  const floating = f.windows[1];
  assert.equal(floating.url, 'file:///test/dist/index.html?application-presentation=1');
  f.invoke('ready', floating);
  await pending;
  f.controller.dispose();
});

test('a failed load rejects activation and leaves a usable workspace', async () => {
  const f = fixture();
  const pending = f.invoke('open', f.owner, f.config);
  f.windows[1].webContents.emit('did-fail-load', {}, -105, 'failed', 'http://test/', true);
  await assert.rejects(pending, /load_failed/);
  assert.equal(f.owner.visible, true);
  assert.equal(f.windows[1].destroyed, true);
  f.controller.dispose();
});

test('sandbox rendering failure restores the workspace before and after activation', async () => {
  for (const activated of [false, true]) {
    const f = fixture();
    const pending = f.invoke('open', f.owner, f.config);
    const floating = f.windows[1];
    if (activated) {
      f.invoke('ready', floating);
      await pending;
    }
    const rejected = activated ? null : assert.rejects(pending, /render_failed/);
    assert.equal(f.invoke('failed', floating), true);
    await new Promise(resolve => setImmediate(resolve));
    if (rejected) await rejected;
    assert.equal(f.owner.visible, true);
    assert.equal(floating.destroyed, true);
    assert.equal(f.owner.messages.at(-1).value.reason, 'render_failed');
    f.controller.dispose();
  }
});

test('bounds are constrained to the chosen display, including negative screen coordinates', () => {
  assert.deepEqual(clampBounds({ x: -3000, y: 900, width: 700, height: 600 },
    { x: -1920, y: 0, width: 1920, height: 1080 }), { x: -1920, y: 480, width: 700, height: 600 });
  assert.deepEqual(clampBounds({ x: 0, y: 0, width: 960, height: 960 },
    { x: 0, y: 0, width: 800, height: 600 }), { x: 0, y: 0, width: 800, height: 600 });
});

test('window handoff waits for the resource first frame and preserves final screen coordinates', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config);
  const floating = f.windows[1], finalBounds = { ...floating.getBounds() };
  const value = await f.invoke('ready', floating, { durationMs: 1120 });
  assert.equal(value.direction, 'compact');
  assert.deepEqual(value.workArea, { x: 0, y: 0, width: 1440, height: 900 });
  assert.equal(f.owner.visible, true);
  assert.equal(floating.visible, false);
  assert.equal(floating.getBounds().x, finalBounds.x);
  await assert.rejects(f.invoke('transition-start', floating, 'wrong-id'), /STALE_REQUEST/);
  await assert.rejects(f.invoke('transition-finish', floating, value.id), /STALE_REQUEST/);
  await f.invoke('transition-start', floating, value.id);
  await opened;
  assert.equal(f.owner.visible, false);
  assert.equal(floating.visible, true);
  assert.equal(floating.getBounds().width, value.viewport.width);
  const hitTest = f.invoke('hit-test', f.owner, f.config.key, true);
  assert.equal(floating.mouse.ignore, true);
  await f.invoke('transition-finish', floating, value.id);
  await hitTest;
  assert.deepEqual(floating.getBounds(), finalBounds);
  assert.equal(floating.mouse.ignore, false);
  await assert.rejects(f.invoke('transition-finish', floating, value.id), /STALE_REQUEST/);
  f.controller.dispose();
});

test('reverse handoff captures the hidden workspace and restores it only after completion', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  f.invoke('ready', floating); await opened;
  const restoring = f.invoke('transition-workspace', f.owner, f.config.key, { durationMs: 1120 });
  await new Promise(resolve => setImmediate(resolve));
  const value = floating.messages.find(item => item.channel.endsWith(':transition')).value;
  assert.equal(value.direction, 'workspace');
  assert.equal(f.owner.visible, false);
  await f.invoke('transition-start', floating, value.id);
  assert.equal(f.owner.visible, false);
  await f.invoke('transition-finish', floating, value.id);
  assert.equal(await restoring, true);
  assert.equal(f.owner.visible, true);
  assert.equal(floating.destroyed, true);
  f.controller.dispose();
});

test('capture completion cannot resize or close a replacement window', async () => {
  const f = fixture();
  let captured;
  f.owner.webContents.capturePage = () => new Promise(resolve => { captured = resolve; });
  const first = f.invoke('open', f.owner, f.config), stale = f.windows[1];
  const rejected = assert.rejects(first, /replaced/);
  const preparing = f.invoke('ready', stale, { durationMs: 1120 });
  const second = f.invoke('open', f.owner, { ...f.config, key: 'second' }), replacement = f.windows[2];
  const bounds = { ...replacement.getBounds() };
  captured({ isEmpty: () => false, toDataURL: () => 'data:image/png;base64,eA==' });
  await assert.rejects(preparing, /STALE_REQUEST/); await rejected;
  assert.deepEqual(replacement.getBounds(), bounds);
  assert.equal(replacement.destroyed, false);
  f.invoke('ready', replacement); await second;
  f.controller.dispose();
});

test('reduced motion skips screenshots and invalid transition options remain rejected', async () => {
  const f = fixture();
  f.owner.webContents.capturePage = () => { throw new Error('must not capture'); };
  const opened = f.invoke('open', f.owner, { ...f.config, state: { reducedMotion: true } });
  const floating = f.windows[1];
  assert.throws(() => f.invoke('ready', floating, { durationMs: Infinity }), /Invalid/);
  assert.equal(f.invoke('ready', floating, { durationMs: 1120 }), true);
  await opened;
  assert.equal(await f.invoke('transition-workspace', f.owner, f.config.key, { durationMs: 1120 }), true);
  assert.equal(f.owner.visible, true);
  f.controller.dispose();
});

test('an unfinished transition times out and releases the window', async () => {
  const f = fixture(25);
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const value = await f.invoke('ready', floating, { durationMs: 1120 });
  await f.invoke('transition-start', floating, value.id); await opened;
  await new Promise(resolve => setTimeout(resolve, 50));
  assert.equal(f.owner.visible, true);
  assert.equal(floating.destroyed, true);
  assert.equal(f.owner.messages.at(-1).value.reason, 'transition_timeout');
  f.controller.dispose();
});

test('transition geometry covers fullscreen sources and destinations on every side, including negative monitors', () => {
  const workspace = { x: -1920, y: 0, width: 1920, height: 1080 };
  for (const surface of [
    { x: -1920, y: 0, width: 400, height: 720 }, { x: -400, y: 0, width: 400, height: 720 },
    { x: -1920, y: 360, width: 400, height: 720 }, { x: -400, y: 360, width: 400, height: 720 },
    { x: -1160, y: 180, width: 400, height: 720 }, { x: 50, y: 50, width: 400, height: 720 },
  ]) {
    const value = transitionBounds(workspace, surface);
    for (const [local, absolute] of [[value.workspace, workspace], [value.surface, surface]]) {
      assert.equal(value.stage.x + local.x, absolute.x); assert.equal(value.stage.y + local.y, absolute.y);
      assert.ok(local.x >= 0 && local.y >= 0);
      assert.ok(local.x + local.width <= value.stage.width && local.y + local.height <= value.stage.height);
    }
  }
});

test('the source remains covered until the transition presents a frame after becoming visible', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const descriptor = await f.invoke('ready', floating, { durationMs: 1120 });
  const invalidate = floating.webContents.invalidate;
  floating.webContents.invalidate = () => {};
  const starting = f.invoke('transition-start', floating, descriptor.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.owner.visible, true);
  assert.equal(floating.opacity, 0);
  assert.equal(f.controller.restore(), true);
  assert.equal(floating.destroyed, false);
  floating.presented({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(floating.opacity, 1);
  assert.equal(f.owner.visible, true, 'an opacity-zero receipt must not hide the workspace');
  assert.equal(f.windows[2].visible, true);
  floating.presented({});
  await starting; await opened;
  assert.equal(f.owner.visible, false);
  assert.equal(floating.opacity, 1);
  assert.equal(floating.presented, null);
  assert.equal(f.windows[2].visible, false);
  floating.webContents.invalidate = invalidate;
  await f.invoke('transition-finish', floating, descriptor.id);
  f.controller.dispose();
});

test('native fullscreen exits before hiding and is restored behind the covering final frame', async () => {
  const f = fixture();
  f.owner.fullscreen = true;
  let acknowledgeFullscreen;
  f.owner.setFullScreen = enabled => {
    acknowledgeFullscreen = () => { f.owner.fullscreen = enabled; f.owner.emit(enabled ? 'enter-full-screen' : 'leave-full-screen'); };
  };
  const hide = f.owner.hide.bind(f.owner);
  f.owner.hide = () => { assert.equal(f.owner.isFullScreen(), false, 'a fullscreen Space must not be hidden'); hide(); };
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const descriptor = await f.invoke('ready', floating, { durationMs: 1120 });
  const starting = f.invoke('transition-start', floating, descriptor.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.owner.visible, true);
  assert.equal(f.owner.isFullScreen(), true);
  acknowledgeFullscreen(); await starting; await opened;
  assert.equal(f.owner.visible, false); assert.equal(f.owner.isFullScreen(), false);
  await f.invoke('transition-finish', floating, descriptor.id);
  const restoring = f.invoke('transition-workspace', f.owner, f.config.key, { durationMs: 1120 });
  await new Promise(resolve => setImmediate(resolve));
  const reverse = floating.messages.find(item => item.channel.endsWith(':transition')).value;
  await f.invoke('transition-start', floating, reverse.id);
  const finishing = f.invoke('transition-finish', floating, reverse.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(floating.destroyed, false);
  assert.equal(f.owner.visible, true);
  acknowledgeFullscreen(); await finishing; await restoring;
  assert.equal(f.owner.isFullScreen(), true); assert.equal(floating.destroyed, true);
  f.controller.dispose();
});

test('direct handoff also exits and restores native fullscreen without providing an animation image', async () => {
  const f = fixture();
  f.owner.fullscreen = true;
  let captures = 0;
  const capture = f.owner.webContents.capturePage;
  f.owner.webContents.capturePage = (...args) => { captures += 1; return capture(...args); };
  const opened = f.invoke('open', f.owner, { ...f.config, state: { reducedMotion: true } }), floating = f.windows[1];
  assert.equal(await f.invoke('ready', floating, { durationMs: 1120 }), true);
  await opened;
  assert.equal(f.owner.isFullScreen(), false); assert.equal(captures, 0);
  await f.invoke('close', f.owner, f.config.key);
  assert.equal(f.owner.isFullScreen(), true); assert.equal(floating.destroyed, true);
  assert.equal(floating.messages.some(message => message.channel.endsWith(':transition')), false);
  f.controller.dispose();
});

test('closing during a native fullscreen exit cancels its receipt and removes the event listener', async () => {
  const f = fixture();
  f.owner.fullscreen = true;
  f.owner.setFullScreen = () => {};
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const activationFailure = assert.rejects(opened, /shutdown/);
  const descriptor = await f.invoke('ready', floating, { durationMs: 1120 });
  const starting = f.invoke('transition-start', floating, descriptor.id);
  const startFailure = assert.rejects(starting, /shutdown/);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(f.owner.listenerCount('leave-full-screen'), 1);
  f.controller.dispose();
  await startFailure; await activationFailure;
  assert.equal(f.owner.listenerCount('leave-full-screen'), 0);
  assert.equal(floating.destroyed, true);
});

test('compact geometry changes remain covered until the final surface has presented', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const finalBounds = { ...floating.getBounds() };
  const descriptor = await f.invoke('ready', floating, { durationMs: 1120 });
  await f.invoke('transition-start', floating, descriptor.id); await opened;
  const cover = f.windows[2], stageBounds = { ...floating.getBounds() };
  let resized = false;
  const resizing = Promise.resolve(f.invoke('resize', f.owner, f.config.key, 700, 500)).then(() => { resized = true; });
  cover.webContents.invalidate = () => {};
  floating.webContents.invalidate = () => {};
  const finished = f.invoke('transition-finish', floating, descriptor.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.deepEqual(floating.getBounds(), stageBounds);
  assert.equal(cover.opacity, 0);
  assert.equal(cover.visible, true);
  cover.presented({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(cover.opacity, 1);
  assert.equal(cover.destroyed, false);
  assert.deepEqual(floating.getBounds(), stageBounds, 'the final resize must wait for the visible covering frame');
  cover.presented({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(floating.getBounds().width, 700);
  assert.equal(floating.getBounds().height, 500);
  assert.equal(floating.getBounds().x + 700, finalBounds.x + finalBounds.width);
  assert.equal(resized, false);
  floating.presented({});
  await finished; await resizing;
  assert.equal(cover.destroyed, true);
  f.controller.dispose();
});

test('closing during the covered native resize releases both presentation surfaces', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const descriptor = await f.invoke('ready', floating, { durationMs: 1120 });
  await f.invoke('transition-start', floating, descriptor.id); await opened;
  const cover = f.windows[2];
  cover.webContents.invalidate = () => {};
  const finished = f.invoke('transition-finish', floating, descriptor.id);
  const rejected = assert.rejects(finished, /shutdown/);
  await new Promise(resolve => setImmediate(resolve));
  f.controller.dispose(); await rejected;
  assert.equal(cover.destroyed, true);
  assert.equal(floating.destroyed, true);
});

test('temporary transition mouse passthrough restores the latest hit-test intent without a new pointer event', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  f.invoke('hit-test', f.owner, f.config.key, true);
  const descriptor = await f.invoke('ready', floating, { durationMs: 1120 });
  await f.invoke('transition-start', floating, descriptor.id); await opened;
  assert.equal(floating.mouse.ignore, true);
  await f.invoke('transition-finish', floating, descriptor.id);
  assert.equal(floating.mouse.ignore, false);
  f.controller.dispose();
});

test('reverse preparation holds the unchanged companion while its animation window is resized invisibly', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  f.invoke('ready', floating); await opened;
  const originalBounds = { ...floating.getBounds() };
  const restoring = f.invoke('transition-workspace', f.owner, f.config.key, { durationMs: 1120 });
  await new Promise(resolve => setImmediate(resolve));
  const descriptor = floating.messages.find(message => message.channel.endsWith(':transition')).value;
  const cover = f.windows[2];
  assert.deepEqual(cover.getBounds(), originalBounds);
  assert.equal(cover.visible, true);
  assert.equal(cover.opacity, 1);
  assert.equal(floating.visible, false, 'plugin DOM preparation must not be visible');
  const setBounds = floating.setBounds.bind(floating);
  floating.setBounds = bounds => {
    assert.equal(floating.opacity, 0, 'the live companion cannot visibly expand before playback');
    assert.equal(cover.visible, true);
    setBounds(bounds);
  };
  const invalidate = floating.webContents.invalidate;
  floating.webContents.invalidate = () => {};
  const starting = f.invoke('transition-start', floating, descriptor.id);
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(cover.visible, true);
  floating.presented({});
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(floating.opacity, 1);
  assert.equal(cover.visible, true, 'the original companion must survive the first visible-frame receipt');
  floating.presented({}); await starting;
  assert.equal(cover.visible, false);
  floating.webContents.invalidate = invalidate;
  await f.invoke('transition-finish', floating, descriptor.id); await restoring;
  assert.equal(cover.destroyed, true, 'restoring the workspace must release the source cover');
  f.controller.dispose();
});

test('cancelling before playback releases the visible source cover without hiding the workspace', async () => {
  const f = fixture();
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const rejected = assert.rejects(opened, /shutdown/);
  await f.invoke('ready', floating, { durationMs: 1120 });
  const cover = f.windows[2];
  assert.equal(cover.visible, true);
  assert.equal(f.owner.visible, true);
  f.controller.dispose(); await rejected;
  assert.equal(cover.destroyed, true);
  assert.equal(floating.destroyed, true);
});

test('Linux prepares resized content while hidden without relying on unsupported window opacity', async () => {
  const f = fixture(), Window = f.owner.constructor;
  const floating = new Window({ x: 900, y: 400, width: 360, height: 440 });
  floating.show();
  const entry = { window: floating, config: f.config, interactive: true };
  let hiddenCapture = false;
  floating.webContents.capturePage = async (_rect, options) => {
    if (options === undefined && !floating.visible) hiddenCapture = true;
    return { isEmpty: () => false, toDataURL: () => 'data:image/png;base64,c2NyZWVuc2hvdA==' };
  };
  floating.setOpacity = () => { throw new Error('Linux does not support window opacity'); };
  const setBounds = floating.setBounds.bind(floating);
  floating.setBounds = bounds => { assert.equal(floating.visible, false); setBounds(bounds); };
  const transitions = createWindowTransitions({ BrowserWindow: Window, current: () => entry, owner: () => f.owner,
    screen: { getDisplayMatching: () => ({ workArea: { x: 0, y: 25, width: 1440, height: 875 } }) },
    platform: 'linux', activate: () => { floating.show(); f.owner.hide(); },
    close: reason => { transitions.cancel(entry, reason); floating.destroy(); } });
  const descriptor = await transitions.prepare(entry, 'workspace', { durationMs: 1120 });
  await transitions.start(entry, descriptor.id);
  assert.equal(hiddenCapture, true);
  assert.equal(floating.visible, true);
  await transitions.finish(entry, descriptor.id);
  assert.equal(floating.destroyed, true);
  f.controller.dispose();
});

test('fullscreen bootstrap resolves the normal desktop context once before loading an unanimated resource', async () => {
  const f = fixture();
  f.owner.fullscreen = true;
  f.screen.getDisplayMatching = () => ({ workArea: { x: 0, y: 0, width: 1440, height: f.owner.fullscreen ? 1080 : 900 } });
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const first = f.invoke('bootstrap', floating), second = f.invoke('bootstrap', floating);
  assert.equal(first, second);
  const context = await first;
  assert.equal(context.initialWorkArea.height, 900);
  assert.equal(f.owner.isFullScreen(), false);
  const cover = f.windows[2];
  assert.equal(cover.visible, true);
  assert.equal(f.owner.visible, true);
  assert.equal(floating.visible, false);
  assert.equal(await f.invoke('ready', floating), true);
  await opened;
  assert.equal(cover.destroyed, true);
  assert.equal(f.owner.visible, false);
  assert.equal(floating.messages.some(message => message.channel.endsWith(':transition')), false);
  await f.invoke('close', f.owner, f.config.key);
  assert.equal(f.owner.isFullScreen(), true);
  f.controller.dispose();
});

test('failure after fullscreen bootstrap restores fullscreen and releases its source cover', async () => {
  const f = fixture();
  f.owner.fullscreen = true;
  const opened = f.invoke('open', f.owner, f.config), floating = f.windows[1];
  const rejected = assert.rejects(opened, /render_failed/);
  await f.invoke('bootstrap', floating);
  assert.equal(f.owner.isFullScreen(), false);
  f.invoke('failed', floating);
  await new Promise(resolve => setImmediate(resolve)); await rejected;
  assert.equal(f.owner.isFullScreen(), true);
  assert.equal(f.windows[2].destroyed, true);
  assert.equal(floating.destroyed, true);
  f.controller.dispose();
});
