import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { createComponentMock, componentMockFrame, COMPONENT_MOCK_INSTANCE_KEY as key, COMPONENT_MOCK_TARGET as target }
  from '../node_modules/.cache/webui-events/events.mjs';
import { componentCatalog, createProtocolPipeline, DEFAULT_WEBUI_LIMITS, findNormalizedNode }
  from '../node_modules/.cache/webui-events/events.mjs';

const componentCases = JSON.parse(readFileSync(new URL('./fixtures/webuiComponentCases.json', import.meta.url), 'utf8'));
const protocol = createProtocolPipeline(componentCatalog);

const input = (props = {}) => ({ component: 'Input', props, events: { 'ui.change': { id: 'edit', args: { field: 'city' } } } });
function setup(options, limits) {
  const app = createComponentMock(options, limits); app.start();
  const owner = app.runtime.mountSlot(target.session_id, target.slot_id);
  app.transport.injectRenderFrame(componentMockFrame(input(), 0));
  const snapshot = app.runtime.getInstance(key);
  const binding = app.runtime.bindNode(key, snapshot.localFrameId, '0', owner.mountLeaseId);
  const display = () => app.runtime.reportDisplayed(key, snapshot.revision, snapshot.localFrameId);
  return { ...app, owner, snapshot, binding, display };
}

test('events require actual display even at revision zero, and data cannot override trusted fields', async () => {
  const app = setup();
  try {
    assert.equal((await app.binding.emit('ui.change', { value: 'A', isComposing: false })).status, 'inactive');
    app.display();
    assert.equal((await app.binding.emit('ui.change', { value: 'A', isComposing: false })).status, 'sent');
    assert.deepEqual(app.transport.dispatches[0].payload, {
      action_id: 'edit', args: { field: 'city' }, data: { value: 'A', isComposing: false }, node_key: '0', revision: 0,
    });
    for (const [event, data] of [
      ['ui.clear', {}], ['constructor', {}], ['ui.change', { value: 'A', isComposing: false, action_id: 'forged' }],
      ['ui.change', { value: 2, isComposing: false }], ['ui.change', { value: 'a'.repeat(17000), isComposing: false }],
    ]) assert.equal((await app.binding.emit(event, data)).status, 'invalid_action');
    assert.equal(app.transport.dispatches.length, 1);
  } finally { app.dispose(); }
});

test('continuous and repeated values are independent while earlier requests are pending', async () => {
  const app = setup({ autoAccept: false }); app.display();
  try {
    const requests = ['a', 'ab', 'abc', 'abc'].map((value) => app.binding.emit('ui.change', { value, isComposing: false }));
    assert.equal(app.transport.pendingDispatchCount, 4);
    assert.deepEqual(app.transport.dispatches.map((request) => request.payload.data.value), ['a', 'ab', 'abc', 'abc']);
    for (let i = 0; i < 4; i += 1) app.transport.resolveNextDispatch({ accepted: true, status: 'handled' });
    assert.deepEqual((await Promise.all(requests)).map((result) => result.status), ['sent', 'sent', 'sent', 'sent']);
  } finally { app.dispose(); }
});

test('replace and same-revision recreation invalidate callbacks and late display reports', async () => {
  const app = setup(); app.display();
  try {
    app.transport.injectClearFrame({ target, revision: 0 });
    app.transport.injectRenderFrame(componentMockFrame(input(), 0));
    app.display();
    const next = app.runtime.getInstance(key);
    assert.notEqual(next.localFrameId, app.snapshot.localFrameId);
    assert.equal(next.displayedLocalFrameId, null);
    assert.equal((await app.binding.emit('ui.change', { value: 'old', isComposing: false })).status, 'inactive');
    const fresh = app.runtime.bindNode(key, next.localFrameId, '0', app.owner.mountLeaseId);
    app.runtime.reportDisplayed(key, 0, next.localFrameId);
    assert.equal((await fresh.emit('ui.change', { value: 'new', isComposing: false })).status, 'sent');
    app.transport.injectRenderFrame(componentMockFrame(input(), 1));
    assert.equal((await fresh.emit('ui.change', { value: 'old', isComposing: false })).status, 'inactive');
  } finally { app.dispose(); }
});

test('invalid updates retain current frame; failed render and released bindings cannot send', async () => {
  const app = setup(); app.display();
  try {
    app.transport.injectRenderFrame(componentMockFrame(input({ style: {} }), 1));
    assert.equal(app.runtime.getInstance(key).localFrameId, app.snapshot.localFrameId);
    assert.equal((await app.binding.emit('ui.change', { value: 'still valid', isComposing: false })).status, 'sent');
    app.runtime.reportRenderFailure(key, 0, 'crash', app.snapshot.localFrameId);
    assert.equal((await app.binding.emit('ui.change', { value: 'no', isComposing: false })).status, 'inactive');
  } finally { app.dispose(); }
});

test('leases isolate two consumers and disconnect never resurrects a released callback', async () => {
  const app = setup(); app.display();
  try {
    const owner2 = app.runtime.mountSlot(target.session_id, target.slot_id);
    const binding2 = app.runtime.bindNode(key, app.snapshot.localFrameId, '0', owner2.mountLeaseId);
    app.owner.release();
    assert.equal((await app.binding.emit('ui.change', { value: 'old owner', isComposing: false })).status, 'inactive');
    assert.equal((await binding2.emit('ui.change', { value: 'owner2', isComposing: false })).status, 'sent');
    app.transport.setConnectionState('disconnected'); app.transport.setConnectionState('connected');
    assert.equal((await binding2.emit('ui.change', { value: 'reconnected old', isComposing: false })).status, 'inactive');
    owner2.release(); assert.equal(app.runtime.getInstance(key), undefined);
  } finally { app.dispose(); }
});

test('partial consumers release only their owned instances', () => {
  const app = createComponentMock(); app.start();
  try {
    const ownerA = app.runtime.mountSlot(target.session_id, target.slot_id, { instanceKey: 'a' });
    const ownerB = app.runtime.mountSlot(target.session_id, target.slot_id, { instanceKey: 'b' });
    for (const instance_key of ['a', 'b']) app.transport.injectRenderFrame({ ...componentMockFrame(input()), target: { ...target, instance_key } });
    ownerA.release();
    assert.equal(app.runtime.getInstance(key.replace('gallery', 'a')), undefined);
    assert.ok(app.runtime.getInstance(key.replace('gallery', 'b')));
    ownerB.release();
  } finally { app.dispose(); }
});

test('in-flight receipt becomes stale after replacement, without claiming side effects were cancelled', async () => {
  const app = setup({ autoAccept: false }); app.display();
  try {
    const pending = app.binding.emit('ui.change', { value: 'a', isComposing: false });
    app.transport.injectRenderFrame(componentMockFrame(input(), 1));
    app.transport.resolveNextDispatch({ accepted: true, status: 'handled' });
    assert.equal((await pending).status, 'stale');
  } finally { app.dispose(); }
});

test('disabled and readonly inputs cannot emit changes even with a valid binding', async () => {
  for (const props of [{ disabled: true }, { readOnly: true }]) {
    const app = setup();
    try {
      app.transport.injectRenderFrame(componentMockFrame(input(props), 1));
      const snapshot = app.runtime.getInstance(key);
      app.runtime.reportDisplayed(key, 1, snapshot.localFrameId);
      const binding = app.runtime.bindNode(key, snapshot.localFrameId, '0', app.owner.mountLeaseId);
      assert.equal((await binding.emit('ui.change', { value: 'a', isComposing: false })).status, 'inactive');
    } finally { app.dispose(); }
  }
});

test('readonly inputs allow clicks but block changes and enter submissions', async () => {
  const app = setup();
  try {
    app.transport.injectRenderFrame(componentMockFrame({
      component: 'Input', props: { readOnly: true, value: 'city' }, events: {
        'ui.click': { id: 'inspect' }, 'ui.change': { id: 'edit' }, 'ui.press_enter': { id: 'submit' },
      },
    }, 1));
    const snapshot = app.runtime.getInstance(key);
    app.runtime.reportDisplayed(key, 1, snapshot.localFrameId);
    const binding = app.runtime.bindNode(key, snapshot.localFrameId, '0', app.owner.mountLeaseId);
    assert.equal((await binding.emit('ui.click', { value: 'city' })).status, 'sent');
    assert.equal((await binding.emit('ui.change', { value: 'new', isComposing: false })).status, 'inactive');
    assert.equal((await binding.emit('ui.press_enter', { value: 'city' })).status, 'inactive');
    assert.equal(app.transport.dispatches.length, 1);
    assert.equal(app.transport.dispatches[0].event, 'ui.click');
  } finally { app.dispose(); }
});

test('event text values accept the UTF-8 byte limit independently of JSON overhead', async () => {
  const app = setup(); app.display();
  try {
    for (const value of [
      'a'.repeat(DEFAULT_WEBUI_LIMITS.maxTextBytes),
      '城'.repeat(5461) + 'a',
      '\n'.repeat(DEFAULT_WEBUI_LIMITS.maxTextBytes),
    ]) {
      assert.equal(Buffer.byteLength(value, 'utf8'), DEFAULT_WEBUI_LIMITS.maxTextBytes);
      assert.equal((await app.binding.emit('ui.change', { value, isComposing: false })).status, 'sent');
      assert.equal(app.transport.dispatches.at(-1).payload.data.value, value);
      assert.equal((await app.binding.emit('ui.change', { value: value + 'a', isComposing: false })).status, 'invalid_action');
    }
    assert.equal(app.transport.dispatches.length, 3);
  } finally { app.dispose(); }
});

test('event payload byte limits include trusted action metadata and respect runtime limits', async () => {
  const limits = { ...DEFAULT_WEBUI_LIMITS, maxFrameBytes: 512 };
  const app = setup(undefined, limits); app.display();
  try {
    const emptyPayload = {
      action_id: 'edit', args: { field: 'city' }, data: { value: '', isComposing: false }, node_key: '0', revision: 0,
    };
    const value = 'a'.repeat(limits.maxFrameBytes - Buffer.byteLength(JSON.stringify(emptyPayload)));
    assert.equal((await app.binding.emit('ui.change', { value, isComposing: false })).status, 'sent');
    assert.equal(Buffer.byteLength(JSON.stringify(app.transport.dispatches[0].payload)), limits.maxFrameBytes);
    assert.equal((await app.binding.emit('ui.change', { value: value + 'a', isComposing: false })).status, 'invalid_action');
    assert.equal(app.transport.dispatches.length, 1);
  } finally { app.dispose(); }
});

test('the node walker enforces the shared component property cases', () => {
  const parse = (component, props) => {
    const frame = componentMockFrame({ component, props });
    if (component === 'Panel' || component === 'Iframe') {
      frame.target.slot_id = 'conversation.panel.right';
      if (component === 'Panel') frame.root.children = [{ component: 'Text', props: { value: 'Content' } }];
    }
    return protocol.processRenderFrame(frame);
  };
  for (const [component, props] of componentCases.valid) {
    const result = parse(component, props);
    assert.equal(result.ok, true, JSON.stringify({ component, result }));
  }
  for (const [component, props] of componentCases.invalid) {
    assert.equal(parse(component, props).ok, false, `${component}: ${Object.keys(props)}`);
  }
});

test('normalized content is detached and immutable, including action arguments', () => {
  const raw = componentMockFrame({
    component: 'Select', props: { options: [{ value: 'a', label: 'A' }] },
    events: { 'ui.select': { id: 'choose', args: { allowed: ['a'] } } },
  });
  const result = protocol.processRenderFrame(raw);
  assert.equal(result.ok, true);
  raw.root.props.options[0].label = 'mutated';
  raw.root.events['ui.select'].args.allowed.push('b');
  assert.equal(result.value.tree.props.options[0].label, 'A');
  assert.deepEqual(result.value.tree.events['ui.select'].args, { allowed: ['a'] });
  assert.throws(() => { result.value.tree.events['ui.select'].args.allowed.push('c'); });
});

test('Tabs hidden content shares node and depth budgets and can be located by node key', () => {
  const root = { component: 'Tabs', props: { items: [
    { key: 'a.b', label: 'A' },
    { key: 'b', label: 'B', children: [input()] },
  ] } };
  const result = protocol.processRenderFrame(componentMockFrame(root));
  assert.equal(result.ok, true);
  assert.equal(result.value.nodeCount, 2);
  assert.equal(result.value.tree.props.items[1].children, undefined);
  assert.equal(findNormalizedNode(result.value.tree, '0.items.1.children.0').component, 'Input');
  for (const limits of [{ maxNodes: 1 }, { maxDepth: 1 }]) {
    const limited = createProtocolPipeline(componentCatalog, { ...DEFAULT_WEBUI_LIMITS, ...limits });
    assert.equal(limited.processRenderFrame(componentMockFrame(root)).ok, false);
  }
});

test('component event declarations reject legacy actions, extra fields and unsupported events', () => {
  for (const extra of [
    { events: {} }, { events: { 'ui.clear': { id: 'clear' } } }, { events: { 'ui.change': null } },
    { events: { 'ui.change': { id: '   ' } } },
    { events: { 'ui.change': { id: 'edit', args: null } } },
    { events: { 'ui.change': { id: 'edit', extra: true } } },
    { action: { id: 'old' } }, { unsupported: true }, { children: [] },
  ]) {
    assert.equal(protocol.processRenderFrame(componentMockFrame({ component: 'Input', ...extra })).ok, false,
      JSON.stringify(extra));
  }
  assert.equal(protocol.processRenderFrame(componentMockFrame({
    component: 'Select', props: { options: [] }, events: { 'ui.change': { id: 'change' } },
  })).ok, false);
});

test('event args accept bounded nested JSON objects and reject oversized collections', () => {
  const parse = (args) => protocol.processRenderFrame(componentMockFrame({
    component: 'Input', events: { 'ui.change': { id: 'edit', args } },
  }));
  const args = { selection: { values: ['a', false, null, { id: 1 }] } };
  assert.deepEqual(parse(args).value.tree.events['ui.change'].args, args);
  assert.equal(parse({ values: Array(101).fill('a') }).ok, false);
  assert.equal(parse({ value: 'a'.repeat(8192) }).ok, false);
  let deep = {};
  for (let i = 0; i < 25; i += 1) deep = { next: deep };
  assert.equal(parse(deep).ok, false);
});

test('root-only components respect slot restrictions and cannot be nested in Tabs', () => {
  const panel = { component: 'Panel', props: { title: 'Panel' }, children: [{ component: 'Text' }] };
  assert.equal(protocol.processRenderFrame(componentMockFrame(panel)).ok, false);
  const frame = componentMockFrame(panel);
  frame.target.slot_id = 'conversation.panel.right';
  assert.equal(protocol.processRenderFrame(frame).ok, true);
  frame.root = { component: 'Box' };
  assert.equal(protocol.processRenderFrame(frame).ok, false);
  for (const node of [panel, { component: 'Iframe', props: { title: 'Frame', src: '/preview' } }]) {
    assert.equal(protocol.processRenderFrame(componentMockFrame({
      component: 'Tabs', props: { items: [{ key: 'a', label: 'A', children: [node] }] },
    })).ok, false);
  }
});

test('non-JSON values, oversized frames and unsupported target scopes are rejected', () => {
  for (const value of [NaN, Infinity, undefined, () => true, new Date()]) {
    assert.equal(protocol.processRenderFrame(componentMockFrame(input({ value }))).ok, false);
  }
  const cyclic = {}; cyclic.self = cyclic;
  assert.equal(protocol.processRenderFrame(componentMockFrame(input(cyclic))).ok, false);
  assert.equal(protocol.processRenderFrame(componentMockFrame(input({ value: 'a'.repeat(300000) }))).ok, false);
  for (const candidateTarget of [
    { ...target, session_id: '' },
    { ...target, application_id: 'app' },
    { ...target, session_id: '', slot_id: 'application.entry', application_id: 'app' },
  ]) {
    assert.equal(protocol.processRenderFrame({ ...componentMockFrame(input()), target: candidateTarget }).ok, false);
  }
});

test('package consumers keep independent bindings and only their own instances on release', async () => {
  const app = createComponentMock(); app.start();
  try {
    const owners = ['pkg.a', 'pkg.b'].map((packageId) => app.runtime.mountSlot(target.session_id, target.slot_id, { packageId }));
    const bindings = ['pkg.a', 'pkg.b'].map((package_id, index) => {
      app.transport.injectRenderFrame({ ...componentMockFrame(input()), target: { ...target, package_id } });
      const instanceKey = key.replace(target.package_id, package_id);
      const snapshot = app.runtime.getInstance(instanceKey);
      app.runtime.reportDisplayed(instanceKey, 1, snapshot.localFrameId);
      return app.runtime.bindNode(instanceKey, snapshot.localFrameId, '0', owners[index].mountLeaseId);
    });
    owners[0].release();
    assert.equal((await bindings[0].emit('ui.change', { value: 'a', isComposing: false })).status, 'inactive');
    assert.equal((await bindings[1].emit('ui.change', { value: 'b', isComposing: false })).status, 'sent');
    assert.equal(app.runtime.getInstance(key.replace(target.package_id, 'pkg.a')), undefined);
    assert.ok(app.runtime.getInstance(key.replace(target.package_id, 'pkg.b')));
    owners[1].release();
  } finally { app.dispose(); }
});


const eventCases = JSON.parse(readFileSync(new URL('./fixtures/webuiEventCases.json', import.meta.url), 'utf8'));
for (const [index, { component, props, event, data, allowed }] of eventCases.entries()) {
  test(`shared event case ${index}: ${component} ${event} ${allowed ? 'sends' : 'rejects'} ${JSON.stringify(data)}`, async () => {
    const app = createComponentMock(); app.start();
    try {
      const owner = app.runtime.mountSlot(target.session_id, target.slot_id);
      app.transport.injectRenderFrame(componentMockFrame({
        component, props, events: { [event]: { id: 'update' } },
      }));
      const snapshot = app.runtime.getInstance(key);
      assert.ok(snapshot?.frame, 'case must have valid render props');
      app.runtime.reportDisplayed(key, snapshot.revision, snapshot.localFrameId);
      const binding = app.runtime.bindNode(key, snapshot.localFrameId, '0', owner.mountLeaseId);
      assert.equal((await binding.emit(event, data)).status, allowed ? 'sent' : 'invalid_action');
      assert.equal(app.transport.dispatches.length, allowed ? 1 : 0);
    } finally { app.dispose(); }
  });
}
