/**
 * React 接入层：把 Runtime 与渲染注册表通过 Context 提供给槽位宿主。
 * Provider 不拥有 Runtime、不执行启动与销毁；目录与注册表不匹配时在子树挂载前失败。
 */

import { createContext, useContext, useMemo, type ReactNode } from 'react';
import { assertCatalogMatchesRegistry } from '../renderer/validate.ts';
import type { WebUIRendererRegistry } from '../renderer/types.ts';
import type { WebUIRuntime } from '../runtime/types.ts';

export interface WebUIAppContextValue {
  runtime: WebUIRuntime;
  registry: WebUIRendererRegistry;
}

const WebUIAppContext = createContext<WebUIAppContextValue | null>(null);

export interface WebUIProviderProps {
  runtime: WebUIRuntime;
  registry: WebUIRendererRegistry;
  children: ReactNode;
}

export function WebUIProvider({ runtime, registry, children }: WebUIProviderProps) {
  // 在子树挂载前校验目录与注册表精确对应；错配直接失败，不做部分展示。
  useMemo(() => assertCatalogMatchesRegistry(runtime.getCatalog(), registry), [runtime, registry]);
  const value = useMemo<WebUIAppContextValue>(() => ({ runtime, registry }), [runtime, registry]);
  return <WebUIAppContext.Provider value={value}>{children}</WebUIAppContext.Provider>;
}

export function useOptionalWebUIAppContext(): WebUIAppContextValue | null {
  return useContext(WebUIAppContext);
}

export function useWebUIAppContext(): WebUIAppContextValue {
  const context = useContext(WebUIAppContext);
  if (!context) {
    throw new Error('WebUI components must be rendered inside <WebUIProvider>');
  }
  return context;
}

export function useWebUIRuntime(): WebUIRuntime {
  return useWebUIAppContext().runtime;
}

export function useWebUIRendererRegistry(): WebUIRendererRegistry {
  return useWebUIAppContext().registry;
}
