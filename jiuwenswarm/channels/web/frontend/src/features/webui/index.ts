/**
 * 业务侧公开接口：组装入口、Provider/Host、渲染注册表与适配器契约。
 * 协议处理、接入适配与 Runtime 内部实现不在公开面内，业务页面不要依赖。
 */

export { createWebUIAppRuntime } from './assembly/index.ts';
export type { WebUIAppRuntime, WebUIAppRuntimeOptions } from './assembly/index.ts';

export { WebUIProvider, useWebUIRendererRegistry, useWebUIRuntime } from './react/WebUIProvider.tsx';
export type { WebUIAppContextValue, WebUIProviderProps } from './react/WebUIProvider.tsx';
export { WebUIAppRoot } from './react/WebUIAppRoot.tsx';
export type { WebUIAppRootProps } from './react/WebUIAppRoot.tsx';
export { WebUIHost } from './react/WebUIHost.tsx';
export type { WebUIHostProps } from './react/WebUIHost.tsx';
export { WebUISlotProvider } from './react/WebUISlotProvider.tsx';
export { WebUINavigationHost, WebUIPageHost } from './react/WebUINavigation.tsx';
export type { WebUIPageTarget } from './react/WebUISlotContext.ts';
export { useWebUIPanels } from './react/useWebUIPanels.tsx';
export type { WebUIPanelEntry } from './react/useWebUIPanels.tsx';

export { createRendererRegistry } from './renderer/index.ts';
export { createComponentRendererRegistry } from './renderer/adapters/componentRegistry.ts';
export type {
  WebUIAdapterContext,
  WebUIAdapterProps,
  WebUIComponentAdapter,
  WebUIRendererRegistry,
  WebUIComponentStyle,
  WebUIStyleOverrides,
} from './renderer/index.ts';

export type {
  WebUIInteractionIntent,
  WebUIInteractionReceipt,
  WebUIInteractionResult,
  WebUIInteractionStatus,
  WebUINormalizedNode,
  WebUINormalizedTarget,
} from './contracts/index.ts';
export type { WebUICatalog, WebUIComponentRule, WebUIPropRule, WebUISlotRule } from './catalog/index.ts';
