/**
 * 临时渲染壳子校验入口（不属于仓库交付物）：用 react-dom/server 验证
 * 全局渲染器的受限接口、缺失适配器整树失败，以及 Host → 实例视图的接线。
 */

import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import assert from 'node:assert/strict';

import { WebUISchemaRenderer, createRendererRegistry } from '../../src/features/webui/renderer/index.ts';
import { WebUIHost } from '../../src/features/webui/react/WebUIHost.tsx';
import { WebUIProvider } from '../../src/features/webui/react/WebUIProvider.tsx';
import { DEMO_SLOT, demoCatalog } from '../../src/features/webui/testing/demoCatalog.ts';
import { demoRenderFrame } from '../../src/features/webui/testing/fixtures.ts';
import { createProtocolPipeline } from '../../src/features/webui/protocol/index.ts';
import { webUIInstanceKey } from '../../src/features/webui/contracts/normalized.ts';
import { createWebUIRuntime } from '../../src/features/webui/runtime/index.ts';
import { createMockTransport } from '../../src/features/webui/transport/index.ts';

function Probe({ node, children, context }) {
  return createElement(
    'section',
    { 'data-testid': 'probe', 'data-instance': context.instanceKey },
    createElement('span', { 'data-testid': 'probe-title' }, String(node.props.title ?? '')),
    children,
  );
}

const registry = createRendererRegistry({ demo: Probe });
const processed = createProtocolPipeline(demoCatalog).processRenderFrame(demoRenderFrame({ revision: 1 }));
assert.equal(processed.ok, true);
const context = { instanceKey: webUIInstanceKey(processed.value.target), connected: true };
const markup = renderToStaticMarkup(
  createElement(WebUISchemaRenderer, {
    tree: processed.value.tree,
    registry,
    context,
    onIntent: async () => ({ status: 'sent', receipt: { accepted: true, status: 'handled' } }),
    renderGeneration: 1,
  }),
);
assert.ok(markup.includes('data-testid="probe"'));
assert.ok(markup.includes('hello'));

assert.throws(
  () =>
    renderToStaticMarkup(
      createElement(WebUISchemaRenderer, {
        tree: processed.value.tree,
        registry: createRendererRegistry({}),
        context,
        onIntent: async () => ({ status: 'sent' }),
        renderGeneration: 1,
      }),
    ),
  /adapter is missing/,
);

const transport = createMockTransport();
const runtime = createWebUIRuntime({ transport, catalog: demoCatalog });
runtime.start();
runtime.mountSlot('sess-1', DEMO_SLOT);
transport.injectRenderFrame(demoRenderFrame({ revision: 1 }));
const hostMarkup = renderToStaticMarkup(
  createElement(
    WebUIProvider,
    { runtime, registry },
    createElement(WebUIHost, { slotId: DEMO_SLOT, sessionId: 'sess-1' }),
  ),
);
assert.ok(hostMarkup.includes('data-testid="probe"'));
assert.ok(hostMarkup.includes('hello'));

console.log('WEBUI-RENDERER-CHECK: ALL PASSED');
