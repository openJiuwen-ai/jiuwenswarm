/**
 * 协议目录：协议处理层与渲染层共用的类型规则来源。
 * 本期由测试目录注入；后续替换为后端 constants.py 的镜像（组件、槽位、token、限制）。
 */

import type { WebUIUpstreamEvent } from '../contracts/wire.ts';

export type WebUIPropRule =
  | { kind: 'token'; values: readonly string[] }
  | { kind: 'string'; maxBytes?: number }
  | { kind: 'number'; min?: number; max?: number; integer?: boolean }
  | { kind: 'boolean' }
  | { kind: 'string-list'; maxItems?: number }
  | { kind: 'record-list'; maxItems?: number };

export interface WebUIComponentRule {
  name: string;
  props: Readonly<Record<string, WebUIPropRule>>;
  /** 协议允许的默认值；缺省时渲染层使用自己的兜底，不允许协议层静默丢字段。 */
  defaults?: Readonly<Record<string, unknown>>;
  allowChildren: boolean;
  allowAction: boolean;
  /** 交互组件对应的上行事件；allowAction 为 true 时必填。 */
  actionEvent?: WebUIUpstreamEvent;
}

export interface WebUISlotRule {
  id: string;
  display: 'list' | 'single';
}

export interface WebUICatalog {
  getComponent(name: string): WebUIComponentRule | undefined;
  getSlot(slotId: string): WebUISlotRule | undefined;
  listComponents(): readonly WebUIComponentRule[];
  listSlots(): readonly WebUISlotRule[];
}

export interface WebUICatalogConfig {
  components: readonly WebUIComponentRule[];
  slots: readonly WebUISlotRule[];
}

/**
 * 组装目录并在初始化时做一致性检查：重复项、交互组件缺少事件、默认值未声明。
 * 规则不完整时直接抛错，避免“可校验但不可渲染”或相反的情况静默存在。
 */
export function createCatalog(config: WebUICatalogConfig): WebUICatalog {
  const components = new Map<string, WebUIComponentRule>();
  for (const rule of config.components) {
    if (components.has(rule.name)) {
      throw new Error(`duplicate webui component rule: ${rule.name}`);
    }
    if (rule.allowAction && !rule.actionEvent) {
      throw new Error(`component ${rule.name} allows action but has no actionEvent`);
    }
    if (!rule.allowAction && rule.actionEvent) {
      throw new Error(`component ${rule.name} declares actionEvent but does not allow action`);
    }
    for (const propName of Object.keys(rule.defaults ?? {})) {
      if (!(propName in rule.props)) {
        throw new Error(`component ${rule.name} default prop is not declared: ${propName}`);
      }
    }
    components.set(rule.name, rule);
  }

  const slots = new Map<string, WebUISlotRule>();
  for (const slot of config.slots) {
    if (slots.has(slot.id)) {
      throw new Error(`duplicate webui slot rule: ${slot.id}`);
    }
    slots.set(slot.id, slot);
  }

  return {
    getComponent: (name) => components.get(name),
    getSlot: (slotId) => slots.get(slotId),
    listComponents: () => [...components.values()],
    listSlots: () => [...slots.values()],
  };
}
