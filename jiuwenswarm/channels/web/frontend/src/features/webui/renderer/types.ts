/**
 * 组件渲染层接口：适配器只接收节点、子内容、必要渲染上下文和 dispatch。
 * 适配器不获取 Runtime、通信客户端或完整实例快照；dispatch 返回统一结果。
 */

import type { ComponentType, ReactNode } from 'react';
import type { WebUIInteractionIntent, WebUIInteractionResult } from '../contracts/interaction.ts';
import type { WebUINormalizedNode } from '../contracts/normalized.ts';

/** 渲染上下文：只包含适配器真正需要的稳定信息。 */
export interface WebUIAdapterContext {
  instanceKey: string;
  connected: boolean;
}

export interface WebUIAdapterProps {
  node: WebUINormalizedNode;
  context: WebUIAdapterContext;
  children: ReactNode;
  /** 派发当前节点声明的 action；永不 reject，异常统一转换为 failed。 */
  dispatch: (payload?: Record<string, unknown>) => Promise<WebUIInteractionResult>;
}

export type WebUIComponentAdapter = ComponentType<WebUIAdapterProps>;

export type WebUIIntentHandler = (intent: WebUIInteractionIntent) => Promise<WebUIInteractionResult>;

export interface WebUIRendererRegistry {
  get(component: string): WebUIComponentAdapter | undefined;
  has(component: string): boolean;
  list(): readonly string[];
}

/** 注册表只读：宿主组装时一次创建，不在运行期做全局注册。 */
export function createRendererRegistry(
  adapters: Readonly<Record<string, WebUIComponentAdapter>>,
): WebUIRendererRegistry {
  const entries = new Map<string, WebUIComponentAdapter>(Object.entries(adapters));
  return {
    get: (component) => entries.get(component),
    has: (component) => entries.has(component),
    list: () => [...entries.keys()],
  };
}
