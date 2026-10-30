/**
 * 组件渲染层接口：适配器只接收节点、子内容、必要渲染上下文和 emit。
 * 适配器不获取 Runtime、通信客户端或完整实例快照；emit 返回统一结果。
 */

import type { ComponentType, CSSProperties, ReactNode } from 'react';
import type { WebUIInteractionResult } from '../contracts/interaction.ts';
import type { WebUINormalizedNode } from '../contracts/normalized.ts';
import type { WebUINodeBinding } from '../runtime/types.ts';
import type { WebUIComponentStateStore } from './componentState.ts';

/** Trusted host presentation; these values never come from the plugin wire schema. */
export interface WebUIComponentStyle {
  className?: string;
  style?: CSSProperties;
  /** Box/Button may use an unstyled native element instead of library defaults. */
  unstyled?: boolean;
  activeClassName?: string;
  icon?: ReactNode;
  iconClassName?: string;
  labelClassName?: string;
  hideLabel?: boolean;
}

export type WebUIStyleOverrides = Readonly<Record<string, WebUIComponentStyle>>;

/** 渲染上下文：只包含适配器真正需要的稳定信息。 */
export interface WebUIAdapterContext {
  instanceKey: string;
  /** Trusted frame scope, supplied by the instance view rather than plugin props. */
  sessionId?: string;
  connected: boolean;
  /** Optional state port; each component owns the values it chooses to retain. */
  componentState?: WebUIComponentStateStore;
  styles?: WebUIStyleOverrides;
  selected?: boolean;
}

export interface WebUIAdapterProps {
  node: WebUINormalizedNode;
  context: WebUIAdapterContext;
  children: ReactNode;
  /** Host styles take precedence over component defaults. */
  appearance?: WebUIComponentStyle;
  emit: (event: string, data: Record<string, unknown>) => Promise<WebUIInteractionResult>;
  contentGroups: Readonly<Record<string, ReactNode>>;
}

export type WebUINodeBinder = (nodeKey: string) => WebUINodeBinding;

export type WebUIComponentAdapter = ComponentType<WebUIAdapterProps>;

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
