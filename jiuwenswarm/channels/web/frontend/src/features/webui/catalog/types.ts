/**
 * 协议目录：协议处理层与渲染层共用的类型规则来源。
 * 生产组件与测试组件使用同一事件契约登记；槽位、token 和限制与后端同步。
 */

import { WEBUI_COMPONENT_EVENTS } from '../contracts/wire.ts';

export type WebUIPropRule =
  | { kind: 'token'; values: readonly string[] }
  | { kind: 'string'; maxBytes?: number }
  | { kind: 'number'; min?: number; max?: number; integer?: boolean }
  | { kind: 'boolean' }
  | { kind: 'string-list'; maxItems?: number }
  | { kind: 'record-list'; maxItems?: number }
  | { kind: 'null' }
  | { kind: 'array'; item: WebUIPropRule; maxItems?: number }
  | { kind: 'object'; fields: Readonly<Record<string, WebUIPropRule>>; required?: readonly string[] }
  | { kind: 'record'; value: WebUIPropRule; maxItems?: number }
  | { kind: 'union'; choices: readonly WebUIPropRule[] }
  | { kind: 'lazy'; get: () => WebUIPropRule }
  | { kind: 'nodes' };

export interface WebUIComponentRule {
  name: string;
  props: Readonly<Record<string, WebUIPropRule>>;
  /** 协议允许的默认值；缺省时渲染层使用自己的兜底，不允许协议层静默丢字段。 */
  defaults?: Readonly<Record<string, unknown>>;
  allowChildren: boolean;
  required?: readonly string[];
  events?: Readonly<Record<string, WebUIPropRule>>;
  /** Only the Tabs catalog declares this restricted content location. */
  content?: { prop: string; children: string };
  childComponents?: readonly string[];
  validate?: (props: Readonly<Record<string, unknown>>) => string | null;
  /** Validate component event semantics after the common payload shape checks. */
  validateEvent?: (
    event: string,
    data: Readonly<Record<string, unknown>>,
    props: Readonly<Record<string, unknown>>,
  ) => string | null;
  prepare?: (props: Record<string, unknown>) => Record<string, unknown>;
}

export interface WebUISlotRule {
  id: string;
  display: 'list' | 'single';
  scope?: 'session' | 'application';
  /** list 槽位最多同时渲染的贡献实例数；缺省不限制。 */
  maxVisible?: number;
  /** Panel/Iframe 等专用根组件的许可；普通组件仍可作为根。右侧面板使用此完整白名单。 */
  rootComponents?: readonly string[];
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
 * 组装目录并在初始化时做一致性检查：重复项、非法事件、默认值未声明。
 * 规则不完整时直接抛错，避免“可校验但不可渲染”或相反的情况静默存在。
 */
export function createCatalog(config: WebUICatalogConfig): WebUICatalog {
  const components = new Map<string, WebUIComponentRule>();
  for (const rule of config.components) {
    if (components.has(rule.name)) {
      throw new Error(`duplicate webui component rule: ${rule.name}`);
    }
    for (const event of Object.keys(rule.events ?? {})) {
      if (!(WEBUI_COMPONENT_EVENTS as readonly string[]).includes(event)) {
        throw new Error(`component ${rule.name} declares unsupported event: ${event}`);
      }
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
