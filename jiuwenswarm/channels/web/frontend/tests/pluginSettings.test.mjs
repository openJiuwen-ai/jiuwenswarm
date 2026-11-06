import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import test from 'node:test';
import { JSDOM } from 'jsdom';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import {
  orderSettingsNodes,
  composePluginSettings,
  readSettingsCatalog,
  readPreference,
  writePreference,
  componentCatalog,
  createProtocolPipeline,
  WebUISchemaRenderer,
  componentRendererRegistry,
  createWebUIRuntime,
  createMockTransport,
} from '../node_modules/.cache/webui-components/components.mjs';

const added = (id, location = {}) => ({ value: { id }, location });
const ids = (values) => values.map((node) => node.id);

test('location supports adjacent native anchors, chains and append defaults', () => {
  const result = orderSettingsNodes(
    [{ id: 'a' }, { id: 'b' }],
    [added('tail'), added('x', { after: 'a' }), added('y', { after: 'x' }), added('z', { before: 'b' })],
  );
  assert.deepEqual(ids(result.values), ['a', 'x', 'y', 'z', 'b', 'tail']);
  assert.deepEqual(result.errors, []);
});

test('missing sibling anchors append; valid mutually-redundant constraints are accepted', () => {
  const result = orderSettingsNodes(
    [{ id: 'a' }],
    [added('x', { after: 'missing' }), added('y', { after: 'z' }), added('z', { before: 'y' })],
  );
  assert.deepEqual(ids(result.values), ['a', 'x', 'z', 'y']);
  assert.deepEqual(result.errors, []);
});

test('real ordering cycles and duplicate IDs do not remove unrelated native nodes', () => {
  const result = orderSettingsNodes(
    [{ id: 'a' }],
    [added('x', { after: 'y' }), added('y', { after: 'x' }), added('good'), added('a')],
  );
  assert.deepEqual(ids(result.values), ['a', 'good']);
  assert.deepEqual(new Set(result.errors.map(({ code }) => code)), new Set(['ORDER_CYCLE', 'DUPLICATE_ID']));
});

test('composition resolves three levels regardless of declaration order and isolates missing parents', () => {
  const accessPolicy = { evaluate: () => ({ level: 'editable' }) };
  const base = {
    id: 'native',
    compositionMode: 'base',
    accessPolicy,
    modules: [
      {
        id: 'general',
        titleKey: 'general',
        icon: 'svg',
        sections: [{ id: 'display', items: [{ id: 'native', component: 'custom', render: () => null }] }],
      },
    ],
  };
  const before = JSON.stringify(base);
  const item = (id) => ({ id, component: 'custom', render: () => null });
  const result = composePluginSettings(base, [
    { kind: 'item', value: item('plugin::field'), location: { menu: 'plugin::menu', section: 'plugin::tools' } },
    {
      kind: 'item',
      value: item('plugin::display'),
      location: { menu: 'general', section: 'display', after: 'missing' },
    },
    { kind: 'item', value: item('invalid'), location: { menu: 'general', section: 'missing' } },
    { kind: 'section', value: { id: 'plugin::tools', items: [] }, location: { menu: 'plugin::menu' } },
    {
      kind: 'menu',
      value: { id: 'plugin::menu', titleKey: 'plugin', icon: 'svg', sections: [] },
      location: { after: 'general' },
    },
  ]);
  assert.deepEqual(ids(result.definition.modules), ['general', 'plugin::menu']);
  assert.deepEqual(ids(result.definition.modules[0].sections[0].items), ['native', 'plugin::display']);
  assert.deepEqual(ids(result.definition.modules[1].sections[0].items), ['plugin::field']);
  assert.deepEqual(result.errors, [{ id: 'invalid', code: 'PARENT_MISSING' }]);
  assert.equal(result.definition.accessPolicy, accessPolicy);
  assert.equal(JSON.stringify(base), before);
});

test('catalog validates location and translations and canonicalizes package order', () => {
  const declaration = {
    package_id: 'z',
    id: 'show',
    kind: 'item',
    location: { menu: 'general', section: 'display' },
    scope: 'application',
    instance_key: 'show',
    presentation: 'row',
    title: { zh: '显示', en: 'Show' },
  };
  const raw = { revision: '1', errors: {}, items: [declaration, { ...declaration, package_id: 'a' }] };
  assert.deepEqual(
    readSettingsCatalog(raw).items.map((item) => item.package_id),
    ['a', 'z'],
  );
  for (const invalid of [
    { ...declaration, title: { zh: '显示' } },
    { ...declaration, scope: 'unknown' },
    { ...declaration, instance_key: '' },
    { ...declaration, location: { menu: 'general' } },
    { ...declaration, location: { ...declaration.location, before: 'a', after: 'b' } },
  ])
    assert.throws(() => readSettingsCatalog({ ...raw, items: [invalid] }));
  assert.throws(
    () => readSettingsCatalog({ ...raw, items: [declaration, { ...declaration, id: 'other' }] }),
    /Duplicate settings render target/,
  );
});

test('the original settings slot accepts both declared scopes and rejects the removed entry', () => {
  const pipeline = createProtocolPipeline(componentCatalog);
  const base = {
    schema_version: 1,
    mode: 'replace',
    revision: 1,
    target: { slot_id: 'application.settings', package_id: 'package', instance_key: 'setting' },
    root: { component: 'Switch', props: { checked: true } },
  };
  assert.equal(pipeline.processRenderFrame({ ...base, target: { ...base.target, session_id: 's1' } }).ok, true);
  assert.equal(pipeline.processRenderFrame({ ...base, target: { ...base.target, scope: 'application' } }).ok, true);
  assert.equal(
    pipeline.processRenderFrame({ ...base, target: { ...base.target, scope: 'application', session_id: 's1' } }).ok,
    false,
  );
  assert.equal(
    pipeline.processRenderFrame({
      ...base,
      target: { ...base.target, slot_id: 'application.settings.content', scope: 'application' },
    }).ok,
    false,
  );
  const content = {
    package_id: 'workbench',
    id: 'settings',
    kind: 'item',
    scope: 'session',
    instance_key: 'workbench',
    presentation: 'content',
    location: { menu: 'general', section: 'display' },
  };
  assert.equal(readSettingsCatalog({ revision: 'content', errors: {}, items: [content] }).items[0].title, undefined);
});

test('clearing a conversation preserves application settings within the same slot', () => {
  const transport = createMockTransport();
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog });
  runtime.start();
  const global = runtime.mountSlot('', 'application.settings', {
    scope: 'application',
    packageId: 'global',
    instanceKey: 'switch',
  });
  const session = runtime.mountSlot('s1', 'application.settings', { packageId: 'workbench', instanceKey: 'workbench' });
  const root = { component: 'Switch', props: { checked: true } };
  try {
    transport.injectRenderFrame({
      schema_version: 1,
      revision: 1,
      mode: 'replace',
      root,
      target: { slot_id: 'application.settings', scope: 'application', package_id: 'global', instance_key: 'switch' },
    });
    transport.injectRenderFrame({
      schema_version: 1,
      revision: 1,
      mode: 'replace',
      root,
      target: { slot_id: 'application.settings', session_id: 's1', package_id: 'workbench', instance_key: 'workbench' },
    });
    assert.equal(runtime.getScopeSnapshot('', 'application.settings').instanceKeys.length, 1);
    assert.equal(runtime.getScopeSnapshot('s1', 'application.settings').instanceKeys.length, 1);
    runtime.clearSession('s1');
    runtime.clearSession('');
    assert.equal(runtime.getScopeSnapshot('', 'application.settings').instanceKeys.length, 1);
    assert.equal(runtime.getScopeSnapshot('s1', 'application.settings').instanceKeys.length, 0);
    assert.throws(() => runtime.mountSlot('', 'conversation.input.above', { scope: 'application' }), /scope/);
  } finally {
    global.release();
    session.release();
    runtime.dispose();
  }
});

const pipeline = createProtocolPipeline(componentCatalog);
const appearanceCases = JSON.parse(
  readFileSync(new URL('./fixtures/webuiAppearanceCases.json', import.meta.url), 'utf8'),
);
for (const sample of appearanceCases) {
  test(`appearance contract: ${sample.name}`, () => {
    const result = pipeline.processRenderFrame({
      schema_version: 1,
      mode: 'replace',
      revision: 1,
      target: { session_id: 's1', slot_id: 'conversation.input.above', package_id: 'paint', instance_key: 'main' },
      root: { component: sample.component, props: sample.props },
    });
    assert.equal(result.ok, sample.valid);
    if (sample.name === 'Box uses shared host styling') {
      const markup = renderToStaticMarkup(
        createElement(WebUISchemaRenderer, {
          tree: result.value.tree,
          registry: componentRendererRegistry,
          context: { instanceKey: 'paint', packageId: 'paint', connected: true },
          renderGeneration: 1,
        }),
      );
      assert.match(markup, /data-testid="webui-box"/);
      assert.doesNotMatch(markup, /data-background|backdrop-filter|box-shadow|linear-gradient/);
    }
  });
}

test('client preferences survive readers and isolate packages; failed writes do not publish values', () => {
  const dom = new JSDOM('', { url: 'http://localhost/' });
  const previous = Object.getOwnPropertyDescriptor(globalThis, 'window');
  Object.defineProperty(globalThis, 'window', { configurable: true, value: dom.window });
  const binding = { key: 'visible', default: true };
  try {
    assert.equal(readPreference('one', binding), true);
    writePreference('one', binding, false);
    assert.equal(readPreference('one', { ...binding }), false);
    assert.equal(readPreference('two', binding), true);
    const original = dom.window.Storage.prototype.setItem;
    dom.window.Storage.prototype.setItem = () => {
      throw new Error('storage unavailable');
    };
    assert.throws(() => writePreference('one', binding, true), /storage unavailable/);
    assert.equal(readPreference('one', binding), false);
    dom.window.Storage.prototype.setItem = original;
  } finally {
    if (previous) Object.defineProperty(globalThis, 'window', previous);
    else delete globalThis.window;
    dom.window.close();
  }
});
