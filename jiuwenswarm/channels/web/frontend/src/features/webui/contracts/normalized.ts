/**
 * 规范化内部模型：协议层的输出，供挂载与实例管理层使用。
 * 与 wire 的差异：目标字段改为驼峰；节点补齐默认值并生成路径 key；events 补齐动作参数。
 */

import type { WebUIPreferenceBinding } from './wire.ts';

export interface WebUINormalizedTarget {
  sessionId: string;
  slotId: string;
  packageId: string;
  instanceKey: string;
  scope?: 'session' | 'application';
}

export interface WebUINormalizedAction {
  id: string;
  args: Record<string, unknown>;
}

export interface WebUINormalizedNode {
  /** 实例内唯一的路径 key，例如根节点 "0"、第一个子节点 "0.0"。 */
  key: string;
  component: string;
  props: Readonly<Record<string, unknown>>;
  children: readonly WebUINormalizedNode[];
  events: Readonly<Record<string, WebUINormalizedAction>>;
  contentGroups?: Readonly<Record<string, readonly WebUINormalizedNode[]>>;
}

export interface WebUINormalizedRenderFrame {
  kind: 'render';
  /** Host-supplied lifetime of a Gateway client package, independent of resource URLs. */
  activationGeneration?: string;
  target: WebUINormalizedTarget;
  revision: number;
  schemaVersion: number;
  tree: WebUINormalizedNode;
  nodeCount: number;
  visibleWhen?: WebUIPreferenceBinding;
}

export interface WebUINormalizedClearFrame {
  kind: 'clear';
  target: WebUINormalizedTarget;
  revision: number;
  reason: string;
}

export interface WebUINormalizedErrorFrame {
  kind: 'error';
  target: WebUINormalizedTarget;
  revision: number;
  code: string;
  message: string;
}

export type WebUINormalizedFrame =
  | WebUINormalizedRenderFrame
  | WebUINormalizedClearFrame
  | WebUINormalizedErrorFrame;

/** 实例键：会话 × 槽位 × 包 × 实例，与后端 RenderTarget.key 的语义一致。 */
export function webUIInstanceKey(target: WebUINormalizedTarget): string {
  return [target.sessionId, target.slotId, target.packageId, target.instanceKey].join('|');
}

/** 槽位挂载键：会话 × 槽位。 */
export function webUISlotKey(sessionId: string, slotId: string): string {
  return `${sessionId}|${slotId}`;
}

/** 在规范化树里按路径 key 查找节点。 */
export function findNormalizedNode(tree: WebUINormalizedNode, key: string): WebUINormalizedNode | null {
  if (tree.key === key) {
    return tree;
  }
  for (const child of [...tree.children, ...Object.values(tree.contentGroups ?? {}).flat()]) {
    const found = findNormalizedNode(child, key);
    if (found) {
      return found;
    }
  }
  return null;
}
