/**
 * 媒体容器与沙箱页面之间的消息协议（Gateway S_07 第 3 节）。
 * 只接受来自当前 iframe 窗口且携带本次令牌的消息；沙箱文档来源为 null，不依据 origin 判断。
 */

export const SURFACE_MESSAGE_TYPE = 'webui.surface';
export const SURFACE_PROTOCOL_VERSION = 1;
export const SURFACE_TOKEN_PARAM = 'webui-surface-token';

export type SurfaceFrameEvent = 'ready' | 'heartbeat' | 'complete' | 'error';
export type SurfaceHostEvent = 'start' | 'context';

const FRAME_EVENTS: ReadonlySet<string> = new Set<SurfaceFrameEvent>(['ready', 'heartbeat', 'complete', 'error']);

export interface SurfaceContext {
  role: string;
  visible: boolean;
  reducedMotion: boolean;
  locale: string;
}

/** A fresh random token for each iframe the host creates. */
export function createSurfaceToken(): string {
  const bytes = new Uint8Array(16);
  globalThis.crypto.getRandomValues(bytes);
  return Array.from(bytes, (byte) => byte.toString(16).padStart(2, '0')).join('');
}

/** Append the token as a fragment parameter, joining an existing fragment with `&`. */
export function withSurfaceToken(src: string, token: string): string {
  const parameter = `${SURFACE_TOKEN_PARAM}=${token}`;
  const hash = src.indexOf('#');
  if (hash < 0) return `${src}#${parameter}`;
  return hash === src.length - 1 ? `${src}${parameter}` : `${src}&${parameter}`;
}

/** Return the frame event, or null when the message is not a valid one for this token. */
export function readSurfaceMessage(data: unknown, token: string): SurfaceFrameEvent | null {
  if (!data || typeof data !== 'object' || Array.isArray(data)) return null;
  const message = data as Record<string, unknown>;
  if (message.type !== SURFACE_MESSAGE_TYPE || message.version !== SURFACE_PROTOCOL_VERSION
    || message.token !== token || typeof message.event !== 'string' || !FRAME_EVENTS.has(message.event)) {
    return null;
  }
  return message.event as SurfaceFrameEvent;
}

export function surfaceHostMessage(event: SurfaceHostEvent, context: SurfaceContext) {
  return { type: SURFACE_MESSAGE_TYPE, version: SURFACE_PROTOCOL_VERSION, event, context };
}
