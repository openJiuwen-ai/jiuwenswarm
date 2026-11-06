const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createAppMarketWindows } = require('../../jiuwenswarm/channels/desktop/electron/app_market_windows.cjs');

function setup(load) {
  const created = [];
  const surface = () => ({ children: new Set(),
    addChildView(view) { this.children.add(view); },
    removeChildView(view) { this.children.delete(view); },
  });
  const owner = new EventEmitter();
  owner.isDestroyed = () => !!owner.destroyed;
  owner.contentView = surface();
  owner.getContentBounds = () => ({ width: 1600, height: 1000 });
  owner.webContents = { getZoomFactor: () => 1.25 };
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      this.contentView = surface();
      created.push(this);
    }
    getContentBounds() { return { width: 1200, height: 800 }; }
    setMenuBarVisibility() {}
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    restore() { this.minimized = false; }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    focus() { this.focused = true; }
    destroy() { this.destroyed = true; this.emit('closed'); }
  }
  class View {
    constructor(options) {
      this.options = options;
      const win = created.at(-1);
      win.appView = this;
      const contents = this.webContents = win.webContents = new EventEmitter();
      contents.session = { setPermissionRequestHandler: handler => { win.permission = handler; }, setPermissionCheckHandler: () => {} };
      contents.setWindowOpenHandler = handler => { win.popup = handler; };
      contents.loadURL = async (url, options) => { win.url = url; win.loads = (win.loads || 0) + 1; if (load) await load(url, options); };
      contents.isDestroyed = () => !!this.destroyed;
      contents.close = options => { this.destroyed = true; this.closeOptions = options; };
      contents.focus = () => { this.focused = true; };
      contents.reload = () => { this.reloads = (this.reloads || 0) + 1; };
    }
    setBounds(bounds) { this.bounds = bounds; }
    setVisible(visible) { this.visible = visible; }
  }
  return { owner, created, ...createAppMarketWindows(Window, View, () => owner, () => 'workswarm.ico') };
}

const deferred = () => {
  let resolve, reject;
  const promise = new Promise((yes, no) => { resolve = yes; reject = no; });
  return { promise, resolve, reject };
};

test('desktop apps use isolated persistent windows without taskbar buttons and with host branding', async () => {
  const { open, created, owner } = setup();
  await open('one', 'http://localhost:8080/?token=x#game', 'Game');
  await open('two', 'http://localhost:8081/', 'Other');
  assert.equal(created[0].visible, true);
  assert.equal(created[0].options.skipTaskbar, true);
  assert.equal(created[0].options.title, 'Game · WorkSwarm');
  assert.equal(created[0].options.icon, 'workswarm.ico');
  assert.equal(created[0].options.parent, process.platform === 'win32' ? undefined : owner);
  const prefs = created[0].appView.options.webPreferences;
  assert.equal(prefs.partition, 'persist:app-market-one');
  assert.notEqual(prefs.partition, created[1].appView.options.webPreferences.partition);
  assert.equal(prefs.nodeIntegration, false);
  assert.equal(prefs.sandbox, true);
  assert.equal(prefs.preload, undefined);
  created[0].webContents.emit('page-title-updated', {}, 'Page');
  assert.equal(created[0].options.title, 'Game · WorkSwarm');
  assert.equal(owner.listenerCount('closed'), 1);
  let permission;
  created[0].permission(null, 'camera', allowed => { permission = allowed; });
  assert.equal(permission, false);
});

test('same app restores its hidden minimized window without reloading; new port uses the same partition', async () => {
  const { open, created } = setup();
  await open('one', 'http://localhost:8080/', 'Game');
  const win = created[0];
  win.minimized = true; win.emit('minimize');
  assert.equal(win.visible, false);
  await open('one', 'http://localhost:8080/', 'Game');
  assert.equal(win.visible, true);
  assert.equal(win.minimized, false);
  assert.equal(win.loads, 1);
  await open('one', 'http://localhost:8081/', 'Game');
  assert.equal(created.length, 1);
  assert.equal(win.url, 'http://localhost:8081/');
});

test('late page completion cannot reopen a window hidden by minimizing', async () => {
  const pending = deferred();
  const { open, created } = setup(url => url.endsWith('/play') ? pending.promise : Promise.resolve());
  await open('one', 'http://localhost:8080/', 'Game');
  const win = created[0];
  win.popup({ url: 'http://localhost:8080/play' });
  win.minimized = true; win.emit('minimize');
  pending.resolve();
  await new Promise(resolve => setImmediate(resolve));
  assert.equal(win.visible, false);
  await open('one', 'http://localhost:8080/', 'Game');
  assert.equal(win.visible, true);
  assert.equal(win.loads, 2);
});

test('stopping closes only the matching window and reopening keeps its partition', async () => {
  const { open, close, created } = setup();
  await open('one', 'http://localhost:8080/');
  await open('two', 'http://localhost:8081/');
  const partition = created[0].appView.options.webPreferences.partition;
  close('one'); close('one');
  assert.equal(created[0].destroyed, true);
  assert.equal(created[1].destroyed, undefined);
  await open('one', 'http://localhost:8080/');
  assert.equal(created[2].appView.options.webPreferences.partition, partition);
  assert.throws(() => close(''), /Invalid application ID/);
});

test('main window destruction closes every app window', async () => {
  const { open, owner, created } = setup();
  await open('one', 'http://localhost:8080/');
  await open('two', 'http://localhost:8081/');
  owner.destroyed = true; owner.emit('closed');
  assert.ok(created.every(win => win.destroyed));
});

test('invalid initial entries never attach app views', async () => {
  const { open, created } = setup();
  for (const url of ['file:///secret', 'javascript:alert(1)', 'https://example.com/', 'http://user:password@localhost/']) {
    await assert.rejects(open('one', url));
  }
  assert.equal(created.length, 0);
});

test('superseded initial failure cannot destroy a view reused by a new entry', async () => {
  const loads = [];
  const { open, created } = setup(() => { const load = deferred(); loads.push(load); return load.promise; });
  const first = open('one', 'http://localhost:8080/').catch(error => error);
  const second = open('one', 'http://localhost:8081/');
  loads[0].reject(new Error('ERR_ABORTED'));
  await first;
  assert.equal(created[0].destroyed, undefined);
  loads[1].resolve();
  await second;
  await open('one', 'http://localhost:8081/');
  assert.equal(loads.length, 2);
});

test('late older completion cannot overwrite the latest entry', async () => {
  const loads = [];
  const { open } = setup(() => { const load = deferred(); loads.push(load); return load.promise; });
  const first = open('one', 'http://localhost:8080/');
  const latest = open('one', 'http://localhost:8081/');
  loads[1].resolve(); await latest;
  loads[0].resolve(); await first;
  const reopened = open('one', 'http://localhost:8081/');
  loads[2]?.resolve(); await reopened;
  assert.equal(loads.length, 2);
});

for (const kind of ['popup', 'native']) {
  test(`${kind} navigation preserves request ownership during initial load`, async () => {
    const firstLoad = deferred();
    let count = 0;
    const { open, created } = setup(() => ++count === 1 ? firstLoad.promise : Promise.resolve());
    const first = open('one', 'http://localhost:8080/').catch(error => error);
    if (kind === 'popup') created[0].popup({ url: 'http://localhost:8080/play' });
    else {
      let prevented = false;
      created[0].webContents.emit('will-navigate', { preventDefault: () => { prevented = true; } }, 'http://localhost:8080/play');
      assert.equal(prevented, false);
    }
    firstLoad.reject(new Error('ERR_ABORTED')); await first;
    await open('one', 'http://localhost:8080/');
    assert.equal(count, kind === 'popup' ? 2 : 1);
    assert.equal(created[0].destroyed, undefined);
  });
}

for (const contentType of ['application/x-www-form-urlencoded', 'multipart/form-data']) {
  test(`popup forwards ${contentType}, referrer and request body`, async () => {
    const loads = [];
    const { open, created } = setup((url, options) => { loads.push({ url, options }); });
    await open('one', 'http://localhost:8080/');
    const postBody = { contentType, boundary: contentType.startsWith('multipart') ? 'boundary' : undefined,
      data: [{ type: 'rawData', bytes: Buffer.from('save=one') }] };
    const referrer = { url: 'http://localhost:8080/', policy: 'no-referrer' };
    assert.deepEqual(created[0].popup({ url: 'http://localhost:8080/save', referrer, postBody }), { action: 'deny' });
    await new Promise(resolve => setImmediate(resolve));
    assert.deepEqual(loads[1].options.postData, postBody.data);
    assert.deepEqual(loads[1].options.httpReferrer, referrer);
    assert.equal(loads[1].options.extraHeaders, `Content-Type: ${contentType}${postBody.boundary ? '; boundary=boundary' : ''}`);
    await open('one', 'http://localhost:8080/');
    assert.equal(loads.length, 2);
    for (const event of ['will-navigate', 'will-redirect']) {
      let prevented = false;
      created[0].webContents.emit(event, { preventDefault: () => { prevented = true; } }, 'file:///secret');
      assert.equal(prevented, true);
    }
  });
}

test('failed initial load disposes its view, while an entry update failure remains retryable', async () => {
  let fail = true;
  const { open, created } = setup(() => { if (fail) throw new Error('Unavailable'); });
  await assert.rejects(open('one', 'http://localhost:8080/'), /Unavailable/);
  assert.equal(created[0].destroyed, true);
  fail = false; await open('one', 'http://localhost:8080/');
  fail = true; await assert.rejects(open('one', 'http://localhost:8081/'), /Unavailable/);
  assert.equal(created[1].destroyed, undefined);
  fail = false; await open('one', 'http://localhost:8081/');
  assert.equal(created.length, 2);
});

test('editing moves the same native page into the owner and restoring moves it back without reload', async () => {
  const { open, setViewport, created, owner, close } = setup();
  await open('one', 'http://localhost:8080/', 'Game');
  const win = created[0];
  const view = win.appView;
  await open('one', 'http://localhost:8080/', 'Game', true);
  assert.equal(win.visible, false);
  assert.equal(win.contentView.children.has(view), false);
  assert.equal(owner.contentView.children.has(view), true);
  assert.equal(view.visible, false);
  assert.equal(setViewport('one', { x: 200, y: 100, width: 600, height: 500 }, true), true);
  assert.deepEqual(view.bounds, { x: 250, y: 125, width: 750, height: 625 });
  assert.equal(view.focused, true);
  setViewport('one', null);
  assert.equal(view.visible, false);
  assert.equal(view.destroyed, undefined);
  await open('one', 'http://localhost:8080/', 'Game');
  assert.equal(win.visible, true);
  assert.equal(win.contentView.children.has(view), true);
  assert.equal(owner.contentView.children.has(view), false);
  assert.deepEqual(view.bounds, { x: 0, y: 0, width: 1200, height: 800 });
  assert.equal(win.loads, 1);
  close('one');
  assert.equal(view.destroyed, true);
  assert.deepEqual(view.closeOptions, { waitForBeforeUnload: false });
});

test('embedded pages hide for invalid bounds and owner close disposes all native contents', async () => {
  const { open, setViewport, created, owner } = setup();
  await open('one', 'http://localhost:8080/', 'Game', true);
  await open('two', 'http://localhost:8081/', 'Other', true);
  setViewport('one', { x: 0, y: 40, width: 800, height: 600 });
  setViewport('two', { x: 0, y: 40, width: 800, height: 600 });
  assert.equal(created[0].appView.visible, false);
  assert.equal(created[1].appView.visible, true);
  setViewport('two', { x: 0, y: 0, width: NaN, height: 600 });
  assert.equal(created[1].appView.visible, false);
  owner.destroyed = true;
  owner.emit('closed');
  assert.ok(created.every(win => win.destroyed && win.appView.destroyed));
});

test('closed standalone page is recreated in the editor with the same partition and supports repeated reloads', async () => {
  const { open, setViewport, reload, created, owner } = setup();
  const url = 'http://localhost:8080/';
  await open('one', url, 'Game', true);
  await open('one', url, 'Game');
  const oldView = created[0].appView;
  created[0].destroy();
  assert.equal(oldView.destroyed, true);
  assert.equal(reload('one'), false);
  await open('one', url, 'Game', true);
  const newView = created[1].appView;
  assert.equal(newView.options.webPreferences.partition, oldView.options.webPreferences.partition);
  assert.equal(owner.contentView.children.has(newView), true);
  assert.equal(setViewport('one', { x: 0, y: 40, width: 800, height: 600 }), true);
  assert.equal(newView.visible, true);
  assert.equal(reload('one'), true);
  assert.equal(reload('one'), true);
  assert.equal(newView.reloads, 2);
  assert.equal(newView.destroyed, undefined);
  assert.equal(created.length, 2);
});
