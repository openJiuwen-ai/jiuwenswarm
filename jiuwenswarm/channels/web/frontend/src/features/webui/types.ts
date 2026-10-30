/**
 * WebUI 扩展协议类型：与后端 extensions_v2/webui/models.py 的帧结构一一对应。
 */

export type WebUISlotId =
  | 'conversation.input.above'
  | 'conversation.input.below'
  | 'conversation.panel.right'
  | 'message.assistant.meta';

export type WebUIActionEvent =
  | 'ui.ready' | 'ui.press' | 'ui.toggle' | 'ui.input' | 'ui.select'
  | 'ui.click' | 'ui.change' | 'ui.press_enter' | 'ui.search' | 'ui.clear';

export type WebUIRenderMode = 'replace' | 'clear';

export interface WebUIRenderTarget {
  session_id: string;
  slot_id: string;
  package_id: string;
  instance_key: string;
}

export interface WebUIRenderAction {
  id: string;
  args?: Record<string, unknown>;
}

export interface WebUIRenderNode {
  component: string;
  props?: Record<string, unknown>;
  children?: WebUIRenderNode[];
  action?: WebUIRenderAction;
  events?: Partial<Record<WebUIActionEvent, WebUIRenderAction>>;
}

export interface WebUIRenderFrame {
  target: WebUIRenderTarget;
  revision: number;
  mode: WebUIRenderMode;
  root: WebUIRenderNode | null;
  schema_version: number;
}

export interface WebUIRenderErrorFrame {
  target: WebUIRenderTarget;
  revision: number;
  code: string;
  message?: string;
}

export interface WebUIRenderClearFrame {
  target: WebUIRenderTarget;
  revision: number;
  reason?: string;
}

export const WEBUI_FRAME_EVENT = 'webui.render';
export const WEBUI_ERROR_EVENT = 'webui.render.error';
export const WEBUI_CLEAR_EVENT = 'webui.render.clear';

/** 槽位容量：single 槽位只展示最新实例。 */
export const WEBUI_SLOT_MODES: Record<WebUISlotId, 'list' | 'single'> = {
  'conversation.input.above': 'list',
  'conversation.input.below': 'single',
  'conversation.panel.right': 'list',
  'message.assistant.meta': 'list',
};

export function isWebUISlotId(value: string): value is WebUISlotId {
  return value in WEBUI_SLOT_MODES;
}

export function webUIInstanceKey(target: WebUIRenderTarget): string {
  return [target.session_id, target.slot_id, target.package_id, target.instance_key].join('|');
}

function readString(source: Record<string, unknown>, key: string): string {
  const value = source[key];
  return typeof value === 'string' ? value : '';
}

/** 把下行 payload 归一化为传输目标；结构非法时返回 null（丢弃该帧）。 */
export function parseWebUITarget(payload: Record<string, unknown>): WebUIRenderTarget | null {
  const raw = payload.target;
  if (!raw || typeof raw !== 'object') {
    return null;
  }
  const target = raw as Record<string, unknown>;
  const sessionId = readString(target, 'session_id');
  const slotId = readString(target, 'slot_id');
  if (!sessionId || !isWebUISlotId(slotId)) {
    return null;
  }
  return {
    session_id: sessionId,
    slot_id: slotId,
    package_id: readString(target, 'package_id'),
    instance_key: readString(target, 'instance_key'),
  };
}

export function parseWebUIRenderFrame(payload: Record<string, unknown>): WebUIRenderFrame | null {
  const target = parseWebUITarget(payload);
  if (!target) {
    return null;
  }
  const revision = payload.revision;
  const mode = payload.mode;
  if (typeof revision !== 'number' || (mode !== 'replace' && mode !== 'clear')) {
    return null;
  }
  const root = payload.root;
  return {
    target,
    revision,
    mode,
    root: root && typeof root === 'object' ? (root as WebUIRenderNode) : null,
    schema_version: typeof payload.schema_version === 'number' ? payload.schema_version : 1,
  };
}

export function parseWebUIRenderErrorFrame(payload: Record<string, unknown>): WebUIRenderErrorFrame | null {
  const target = parseWebUITarget(payload);
  const code = payload.code;
  if (!target || typeof code !== 'string') {
    return null;
  }
  return {
    target,
    revision: typeof payload.revision === 'number' ? payload.revision : 0,
    code,
    message: typeof payload.message === 'string' ? payload.message : undefined,
  };
}

export function parseWebUIRenderClearFrame(payload: Record<string, unknown>): WebUIRenderClearFrame | null {
  const target = parseWebUITarget(payload);
  if (!target) {
    return null;
  }
  return {
    target,
    revision: typeof payload.revision === 'number' ? payload.revision : 0,
    reason: typeof payload.reason === 'string' ? payload.reason : undefined,
  };
}

