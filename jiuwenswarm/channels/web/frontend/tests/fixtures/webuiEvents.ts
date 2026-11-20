import { componentCatalog } from '../../src/features/webui/catalog/componentCatalog.ts';
import { WEBUI_COMPONENT_SCHEMA_VERSION, type WebUIWireComponentNode } from '../../src/features/webui/contracts/wire.ts';
import { DEFAULT_WEBUI_LIMITS, type WebUILimits } from '../../src/features/webui/protocol/limits.ts';
import { createProtocolPipeline } from '../../src/features/webui/protocol/process.ts';
import { createWebUIRuntime } from '../../src/features/webui/runtime/createWebUIRuntime.ts';
import { createMockTransport, type MockTransportOptions } from '../../src/features/webui/transport/mockTransport.ts';

export const COMPONENT_MOCK_TARGET = {
  session_id: 'component-mock', slot_id: 'conversation.input.above', package_id: 'component-demo', instance_key: 'gallery',
};
export const COMPONENT_MOCK_INSTANCE_KEY = 'component-mock|conversation.input.above|component-demo|gallery';

export function componentMockFrame(root: WebUIWireComponentNode, revision = 1) {
  return { target: { ...COMPONENT_MOCK_TARGET }, revision, schema_version: WEBUI_COMPONENT_SCHEMA_VERSION, mode: 'replace', root };
}

/** Real protocol and runtime, with transport as the only substitute. */
export function createComponentMock(options: MockTransportOptions = {}, limits: WebUILimits = DEFAULT_WEBUI_LIMITS) {
  const transport = createMockTransport(options);
  const protocol = createProtocolPipeline(componentCatalog, limits);
  const runtime = createWebUIRuntime({ transport, catalog: componentCatalog, protocol, limits });
  return { transport, runtime, protocol, start: () => runtime.start(), dispose: () => runtime.dispose() };
}

export { componentCatalog, DEFAULT_WEBUI_LIMITS, createProtocolPipeline };
export { findNormalizedNode } from '../../src/features/webui/contracts/normalized.ts';
