/**
 * 生产目录与 schema 组件回归：目录/注册表一致性、Box 的协议规则与布局映射，
 * 以及用 react-dom/server 验证 Box → 子节点 → antd Flex 的真实渲染结果。
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';

import {
  componentRendererRegistry, componentCatalog, createProtocolPipeline, WebUISchemaRenderer,
  assertCatalogMatchesRegistry, WebUIBox, resolveBoxLayout, webUIInstanceKey,
} from '../node_modules/.cache/webui-components/components.mjs';

const pipeline = createProtocolPipeline(componentCatalog);

function renderFrame(root, options = {}) {
  return {
    target: {
      session_id: 'sess-1',
      slot_id: 'conversation.input.above',
      package_id: 'pkg.demo',
      instance_key: 'main',
    },
    revision: 1,
    mode: 'replace',
    schema_version: 1,
    root,
    ...options,
  };
}

function processTree(root) {
  const result = pipeline.processRenderFrame(renderFrame(root));
  assert.equal(result.ok, true, `expected a legal frame, got ${JSON.stringify(result.failure)}`);
  return result.value;
}

function renderMarkup(frame) {
  return renderToStaticMarkup(
    createElement(WebUISchemaRenderer, {
      tree: frame.tree,
      registry: componentRendererRegistry,
      context: { instanceKey: webUIInstanceKey(frame.target), connected: true },
      renderGeneration: frame.revision,
    }),
  );
}

test('registered adapters have matching component rules', () => {
  assertCatalogMatchesRegistry(componentCatalog, componentRendererRegistry);
  const catalogNames = componentCatalog
    .listComponents()
    .map((rule) => rule.name)
    .sort();
  assert.deepEqual(catalogNames, componentRendererRegistry.list().slice().sort());
  assert.ok(catalogNames.includes('Box'));
  for (const name of ['Avatar', 'Box', 'Text', 'Button', 'Input', 'Switch', 'Select', 'TreeSelect', 'Slider', 'Tabs', 'Card', 'Divider', 'Empty', 'Image', 'Tag', 'Panel', 'FileTree', 'FilePreview', 'Iframe']) {
    assert.ok(componentRendererRegistry.has(name), `missing adapter: ${name}`);
  }
});

test('FileTree rejects paths outside the workspace', () => {
  const result = pipeline.processRenderFrame(renderFrame({
    component: 'FileTree',
    props: { treeData: [{ key: 'a', title: 'a', path: '../a', kind: 'file' }] },
  }));
  assert.equal(result.ok, false);
});

test('FilePreview renders plugin supplied text and rejects escaping paths', () => {
  const ok = pipeline.processRenderFrame(renderFrame({
    component: 'FilePreview',
    props: { file: { name: 'notes.txt', path: 'notes.txt', mimeType: 'text/plain', size: 3, content: 'abc' } },
  }));
  assert.equal(ok.ok, true);
  const markup = renderMarkup(ok.value);
  assert.match(markup, /webui-file-preview/);
  assert.match(markup, /abc/);

  const bad = pipeline.processRenderFrame(renderFrame({
    component: 'FilePreview',
    props: { file: { name: 'x', path: '../x', mimeType: 'text/plain', size: 1, content: 'a' } },
  }));
  assert.equal(bad.ok, false);
});

test('display adapters render normalized props and preserve nested content', () => {
  const frame = processTree({
    component: 'Box', children: [
      { component: 'Card', props: { title: 'Summary', extra: 'Details', cover: '/cover.png', hoverable: true },
        children: [{ component: 'Text', props: { value: 'Card body' } }] },
      { component: 'Divider', props: { children: 'Section', variant: 'dashed', titlePlacement: 'start' } },
      { component: 'Empty', props: { image: 'simple', description: 'Nothing here' } },
      { component: 'Image', props: { src: '/image.png', alt: 'Preview', width: 120, preview: false } },
      { component: 'Avatar', props: { url: '/logo.svg', alt: 'User avatar', size: 48, shape: 'circle' } },
      { component: 'Tag', props: { children: 'Link', color: 'success', href: 'https://example.com', target: '_blank' } },
    ],
  });
  const markup = renderMarkup(frame);
  for (const name of ['card', 'divider', 'empty', 'image', 'avatar', 'tag']) {
    assert.match(markup, new RegExp(`data-testid="webui-${name}"`));
  }
  for (const text of ['Summary', 'Details', 'Card body', 'Section', 'Nothing here', 'Preview', 'Link']) {
    assert.ok(markup.includes(text), `missing text: ${text}`);
  }
  assert.match(markup, /rel="noopener noreferrer"/);
  assert.match(markup, /data-testid="webui-card-cover"/);
});

test('Panel renders its body while the host owns its title', () => {
  const result = pipeline.processRenderFrame(renderFrame({
    component: 'Panel', props: { title: 'Host title' },
    children: [{ component: 'Text', props: { value: 'Panel body' } }],
  }, { target: { ...renderFrame(null).target, slot_id: 'conversation.panel.right' } }));
  assert.equal(result.ok, true);
  const markup = renderMarkup(result.value);
  assert.match(markup, /data-testid="webui-panel"/);
  assert.match(markup, /Panel body/);
  assert.doesNotMatch(markup, /Host title/);
});

test('production protocol still rejects unregistered slots', () => {
  const frame = renderFrame({ component: 'Text', props: { value: 'hello' } });
  frame.target.slot_id = 'unknown.slot';
  const result = pipeline.processRenderFrame(frame);
  assert.equal(result.ok, false);
  assert.equal(result.failure.code, 'SCHEMA_ERROR');
});

test('Box accepts children and receives catalog defaults', () => {
  const frame = processTree({
    component: 'Box',
    props: {},
    children: [{ component: 'Text', props: { value: 'hi' } }],
  });
  assert.deepEqual(frame.tree.props, {
    direction: 'row',
    gap: 'md',
    align: 'stretch',
    justify: 'start',
    wrap: false,
  });
  assert.equal(frame.tree.children.length, 1);
  assert.equal(frame.tree.children[0].key, '0.0');
  assert.equal(frame.tree.children[0].component, 'Text');
  assert.equal(frame.tree.children[0].props.value, 'hi');
});

test('Box rejects props that are not in the catalog rule', () => {
  const invalid = [
    [{ direction: 'diagonal' }, /invalid direction/],
    [{ gap: 12 }, /invalid gap/],
    [{ align: 'middle' }, /invalid align/],
    [{ justify: 'space-between' }, /invalid justify/],
    [{ wrap: 'yes' }, /invalid wrap/],
    [{ flex: -1 }, /invalid flex/],
    [{ flex: '1' }, /invalid flex/],
    [{ style: { color: 'red' } }, /props not allowed for Box: style/],
    [{ className: 'x' }, /props not allowed for Box: className/],
  ];
  for (const [props, pattern] of invalid) {
    const result = pipeline.processRenderFrame(renderFrame({ component: 'Box', props }));
    assert.equal(result.ok, false, `expected rejection for ${JSON.stringify(props)}`);
    assert.equal(result.failure.kind, 'reject');
    assert.equal(result.failure.code, 'SCHEMA_ERROR');
    assert.match(result.failure.message, pattern);
  }
});

test('Box does not accept an action', () => {
  const result = pipeline.processRenderFrame(
    renderFrame({ component: 'Box', props: {}, action: { id: 'box.press' } }),
  );
  assert.equal(result.ok, false);
  assert.equal(result.failure.code, 'SCHEMA_ERROR');
  assert.match(result.failure.message, /unknown node field/);
});

test('resolveBoxLayout maps every layout token onto antd Flex props', () => {
  assert.deepEqual(resolveBoxLayout({}), {
    vertical: false,
    gap: 'middle',
    align: 'stretch',
    justify: 'flex-start',
    wrap: false,
    flex: undefined,
  });
  assert.deepEqual(resolveBoxLayout({ direction: 'column', gap: 'none', wrap: true }), {
    vertical: true,
    gap: 0,
    align: 'stretch',
    justify: 'flex-start',
    wrap: true,
    flex: undefined,
  });
  assert.deepEqual(
    resolveBoxLayout({ gap: 'sm', align: 'start', justify: 'between', flex: 1 }),
    { vertical: false, gap: 'small', align: 'flex-start', justify: 'space-between', wrap: false, flex: 1 },
  );
  assert.deepEqual(
    resolveBoxLayout({ gap: 'lg', align: 'end', justify: 'around', flex: 2 }),
    { vertical: false, gap: 'large', align: 'flex-end', justify: 'space-around', wrap: false, flex: 2 },
  );
  assert.deepEqual(
    resolveBoxLayout({ align: 'center', justify: 'end' }).justify,
    'flex-end',
  );
  assert.deepEqual(resolveBoxLayout({ align: 'baseline', justify: 'evenly' }).align, 'baseline');
  assert.deepEqual(resolveBoxLayout({ justify: 'center' }).justify, 'center');
  // 未登记取值一律回落到默认布局，交给协议层之前也不会污染宿主样式。
  assert.deepEqual(resolveBoxLayout({ direction: 'diagonal', gap: 'huge', align: 'middle', justify: 'nope' }), {
    vertical: false,
    gap: 'middle',
    align: 'stretch',
    justify: 'flex-start',
    wrap: false,
    flex: undefined,
  });
});

test('Box renders its children through the production registry', () => {
  const frame = processTree({
    component: 'Box',
    props: { direction: 'column', gap: 'lg', align: 'center', justify: 'between', wrap: true, flex: 2 },
    children: [
      { component: 'Text', props: { value: '第一行' } },
      { component: 'Button', props: { label: '确认', variant: 'primary' }, events: { 'ui.click': { id: 'confirm' } } },
    ],
  });
  const markup = renderMarkup(frame);
  assert.match(markup, /data-testid="webui-box"/);
  assert.match(markup, /ant-flex-vertical/);
  assert.match(markup, /ant-flex-gap-large/);
  assert.match(markup, /ant-flex-align-center/);
  assert.match(markup, /ant-flex-justify-space-between/);
  assert.match(markup, /ant-flex-wrap-wrap/);
  assert.match(markup, /style="[^"]*flex:\s*2/);
  assert.match(markup, /data-testid="webui-text"/);
  assert.match(markup, /第一行/);
  assert.match(markup, /data-testid="webui-button"/);
  assert.match(markup, /确认/);
  assert.ok(
    markup.indexOf('data-testid="webui-text"') < markup.indexOf('data-testid="webui-button"'),
    'children keep their declared order',
  );
});

test('a row Box keeps the horizontal direction and default layout', () => {
  const frame = processTree({
    component: 'Box',
    children: [{ component: 'Text', props: { value: 'a' } }, { component: 'Text', props: { value: 'b' } }],
  });
  const markup = renderMarkup(frame);
  assert.doesNotMatch(markup, /ant-flex-vertical/);
  assert.match(markup, /ant-flex-gap-middle/);
  assert.match(markup, /ant-flex-justify-flex-start/);
  assert.doesNotMatch(markup, /ant-flex-wrap-wrap/);
});

test('Box adapter tolerates unnormalized props when used directly', () => {
  const markup = renderToStaticMarkup(
    createElement(WebUIBox, {
      node: { key: '0', component: 'Box', props: { direction: 'row', gap: 'sm' }, children: [], events: {} },
      context: { instanceKey: 'x', connected: true },
      children: null,
      emit: async () => ({ status: 'failed' }),
    }),
  );
  assert.match(markup, /data-testid="webui-box"/);
  assert.match(markup, /ant-flex-gap-small/);
});
