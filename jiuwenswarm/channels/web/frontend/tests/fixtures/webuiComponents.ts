import { createCatalog } from '../../src/features/webui/catalog/types.ts';
import { componentCatalog as contractCatalog } from '../../src/features/webui/catalog/componentCatalog.ts';
import { componentRendererRegistry } from '../../src/features/webui/renderer/adapters/componentRegistry.ts';

export { componentRendererRegistry };
export const componentCatalog = createCatalog({
  components: componentRendererRegistry.list().map((name) => {
    const rule = contractCatalog.getComponent(name);
    if (!rule) throw new Error(`Missing component rule: ${name}`);
    return rule;
  }),
  slots: contractCatalog.listSlots(),
});
export { createProtocolPipeline } from '../../src/features/webui/protocol/process.ts';
export { WebUISchemaRenderer, assertCatalogMatchesRegistry, WebUIContentActivity } from '../../src/features/webui/renderer/index.ts';
export { WebUIBox, resolveBoxLayout } from '../../src/features/webui/renderer/adapters/webuiBox/WebUIBox.tsx';
export { webUIInstanceKey } from '../../src/features/webui/contracts/normalized.ts';
export { createWebUIRuntime } from '../../src/features/webui/runtime/index.ts';
export { createWebUIAppRuntime } from '../../src/features/webui/assembly/index.ts';
export { createMockTransport } from '../../src/features/webui/transport/index.ts';
export { WebUIHost } from '../../src/features/webui/react/WebUIHost.tsx';
export { WebUIEntryHost } from '../../src/features/webui/react/WebUIEntryHost.tsx';
export { WebUIProvider } from '../../src/features/webui/react/WebUIProvider.tsx';
export { WebUISlotProvider } from '../../src/features/webui/react/WebUISlotProvider.tsx';
export { WebUIThemeProvider, resolveWebUITheme } from '../../src/features/webui/react/WebUIThemeProvider.tsx';
export { default as i18n } from '../../src/i18n/index.ts';
export { FileTree } from '../../src/features/webui/renderer/adapters/webuiFileTree/FileTree.tsx';
export { createComponentState } from '../../src/features/webui/renderer/componentState.ts';
