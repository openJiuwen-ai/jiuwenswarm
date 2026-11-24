import { createCatalog } from '../../src/features/webui/catalog/types.ts';
import { componentCatalog as contractCatalog } from '../../src/features/webui/catalog/componentCatalog.ts';
import { componentRendererRegistry } from '../../src/features/webui/renderer/adapters/componentRegistry.ts';

export { componentRendererRegistry };
export { buttonIcon } from '../../src/features/webui/renderer/adapters/webuiButton/buttonIcon.tsx';
export { browserClientCapabilities } from '../../src/features/webui/client/browserClient.ts';
export { GatewayImage } from '../../src/features/webui/client/GatewayResources.tsx';
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
export { WebUIBackgroundHost } from '../../src/features/webui/react/WebUIBackgroundHost.tsx';
export { WebUIAppearanceHost } from '../../src/features/webui/react/WebUIAppearanceHost.tsx';
export * as appearanceModule from '../../src/features/webui/appearance/index.ts';
export * as splashModule from '../../src/features/webui/splash/index.ts';
export { WebUISplashGate } from '../../src/features/webui/react/WebUISplashGate.tsx';
export { MediaSurfacePolicyContext } from '../../src/features/webui/renderer/adapters/webuiMediaSurface/hostPolicy.ts';
export { WebUIProvider } from '../../src/features/webui/react/WebUIProvider.tsx';
export { ApplicationPresentationHost } from '../../src/features/webui/presentation/ApplicationPresentationHost.tsx';
export { SystemPluginRecoverySettings } from '../../src/features/settings/modules/systemPluginRecovery/SystemPluginRecoverySettings.tsx';
export { WebUISlotProvider } from '../../src/features/webui/react/WebUISlotProvider.tsx';
export { WebUINavigationHost, WebUIPageHost } from '../../src/features/webui/react/WebUINavigation.tsx';
export { WebUIThemeProvider, resolveWebUITheme } from '../../src/features/webui/react/WebUIThemeProvider.tsx';
export { default as i18n } from '../../src/i18n/index.ts';
export { FileTree } from '../../src/features/webui/renderer/adapters/webuiFileTree/FileTree.tsx';
export { createComponentState } from '../../src/features/webui/renderer/componentState.ts';
export { orderSettingsNodes, composePluginSettings } from '../../src/features/settings/registry/pluginLocations.ts';
export { readSettingsCatalog } from '../../src/features/settings/registry/usePluginSettings.tsx';
export { readPreference, writePreference } from '../../src/features/webui/client/preferences.ts';
export { usePluginSettings } from '../../src/features/settings/registry/usePluginSettings.tsx';
export { SettingsServicesProvider } from '../../src/features/settings/services/SettingsServicesProvider.tsx';
export { panelNavigation, requestPanelOpen } from '../../src/features/webui/client/panelNavigation.ts';
export { useWebUIPanels } from '../../src/features/webui/react/useWebUIPanels.tsx';
export * as transfersModule from '../../src/features/webui/transfers/index.ts';
export { WebUIUploadContext } from '../../src/features/webui/renderer/adapters/webuiFileImport/uploadContext.ts';
