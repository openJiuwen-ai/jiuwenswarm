const test = require('node:test');
const assert = require('node:assert/strict');
const { EventEmitter } = require('node:events');
const { createAppMarketWindows } = require('../../jiuwenswarm/channels/desktop/electron/app_market_windows.cjs');

function setup(load) {
  const created = [];
  const owner = new EventEmitter();
  owner.isDestroyed = () => !!owner.destroyed;
  class Window extends EventEmitter {
    constructor(options) {
      super();
      this.options = options;
      const contents = this.webContents = new EventEmitter();
      contents.session = { setPermissionRequestHandler: handler => { this.permission = handler; }, setPermissionCheckHandler: () => {} };
      contents.setWindowOpenHandler = handler => { this.popup = handler; };
      contents.loadURL = async (url, options) => { this.url = url; this.loads = (this.loads || 0) + 1; if (load) await load(url, options); };
      created.push(this);
    }
    loadURL(...args) { return this.webContents.loadURL(...args); }
    setMenuBarVisibility() {}
    isDestroyed() { return !!this.destroyed; }
    isMinimized() { return !!this.minimized; }
    restore() { this.minimized = false; }
    show() { this.visible = true; }
    hide() { this.visible = false; }
    focus() { this.focused = true; }
    destroy() { this.destroyed = true; this.webContents = undefined; this.emit('closed'); }
  }
  return { owner, created, ...createAppMarketWindows(Window, () => owner, () => 'workswarm.ico') };
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
  const prefs = created[0].options.webPreferences;
  assert.equal(prefs.partition, 'persist:app-market-one');
  assert.notEqual(prefs.partition, created[1].options.webPreferences.partition);
  assert.equal(prefs.nodeIntegration, false);
  assert.equal(prefs.sandbox, true);
  assert.equal(prefs.preload, undefined);
  let prevented = false;
  created[0].emit('page-title-updated', { preventDefault: () => { prevented = true; } }, 'Page');
  assert.equal(prevented, true);
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
  const partition = created[0].options.webPreferences.partition;
  close('one'); close('one');
  assert.equal(created[0].destroyed, true);
  assert.equal(created[1].destroyed, undefined);
  await open('one', 'http://localhost:8080/');
  assert.equal(created[2].options.webPreferences.partition, partition);
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
