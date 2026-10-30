/**
 * WebUI wire 类型：与后端 extensions_v2/webui/models.py 的帧字段一一对应。
 * 这里只描述结构，所有解析入口仍以 unknown 接收后校验，不能直接类型断言。
 */

export type WebUIWireRenderMode = 'replace' | 'clear';

export interface WebUIWireTarget {
  session_id?: string;
  scope?: 'session' | 'application';
  slot_id: string;
  package_id: string;
  instance_key: string;
}

export interface WebUIWireAction {
  id: string;
  args?: Record<string, unknown>;
}

export interface WebUIWireNode {
  component: string;
  props?: Record<string, unknown>;
  children?: WebUIWireNode[];
  events?: Partial<Record<WebUIComponentEvent, WebUIWireAction>>;
}

export interface WebUIWireRenderFrame {
  target: WebUIWireTarget;
  revision: number;
  mode: WebUIWireRenderMode;
  root: WebUIWireNode | null;
  schema_version: number;
}

export interface WebUIWireClearFrame {
  target: WebUIWireTarget;
  revision: number;
  reason?: string;
}

export interface WebUIWireErrorFrame {
  target: WebUIWireTarget;
  revision: number;
  code: string;
  message?: string;
}

/** 下行事件名，与后端 constants.py 的 FRAME_* 一致。 */
export const WEBUI_RENDER_EVENT = 'webui.render';
export const WEBUI_CLEAR_EVENT = 'webui.render.clear';
export const WEBUI_ERROR_EVENT = 'webui.render.error';

/** 上行唯一入口方法名，与后端 ReqMethod.WEBUI_DISPATCH 一致。 */
export const WEBUI_DISPATCH_METHOD = 'webui.dispatch';

/** The component contract uses schema_version 1 across all render entry points. */
export const WEBUI_COMPONENT_SCHEMA_VERSION = 1;
export const WEBUI_COMPONENT_EVENTS = ['ui.click', 'ui.change', 'ui.press_enter', 'ui.search', 'ui.clear', 'ui.select'] as const;
export type WebUIComponentEvent = (typeof WEBUI_COMPONENT_EVENTS)[number];

export type WebUIWireComponentNode = WebUIWireNode;
