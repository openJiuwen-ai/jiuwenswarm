/**
 * 目录与注册表一致性校验：两者必须精确对应，缺失或多余实现都视为宿主配置错误。
 * Provider 与真实组装入口共用，保证子树挂载前失败。
 */

import type { WebUICatalog } from '../catalog/types.ts';
import type { WebUIRendererRegistry } from './types.ts';

export function assertCatalogMatchesRegistry(catalog: WebUICatalog, registry: WebUIRendererRegistry): void {
  const catalogNames = new Set(catalog.listComponents().map((rule) => rule.name));
  const adapterNames = new Set(registry.list());
  for (const name of catalogNames) {
    if (!adapterNames.has(name)) {
      throw new Error(`WebUI renderer adapter is missing for component: ${name}`);
    }
  }
  for (const name of adapterNames) {
    if (!catalogNames.has(name)) {
      throw new Error(`WebUI renderer adapter has no catalog rule: ${name}`);
    }
  }
}
