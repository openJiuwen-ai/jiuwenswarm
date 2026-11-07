/**
 * 真实接入适配器：包装既有 webClient，不发第二条连接。
 * 通过构造参数注入客户端，便于单测；生产组装时传入 webClient 单例。
 */

import {
  WEBUI_CLEAR_EVENT,
  WEBUI_DISPATCH_METHOD,
  WEBUI_ERROR_EVENT,
  WEBUI_RENDER_EVENT,
} from '../contracts/wire.ts';
import { WEBUI_APPEARANCE_EVENT } from '../appearance/appearance.ts';
import type {
  WebUIConnectionState,
  WebUIDispatchReceipt,
  WebUITransport,
  WebUITransportHandlers,
} from './types.ts';
import type { ClientCapabilityHost } from '../client/createClientCapabilityHost.ts';

export interface WebUIClientEvent {
  payload: Record<string, unknown>;
}

/** webClient 中与接入适配层相关的最小接口。 */
export interface WebUIClientLike {
  on(eventName: string, handler: (event: WebUIClientEvent) => void): () => void;
  onStateChange(handler: (state: string) => void): () => void;
  getState(): string;
  request<T = unknown>(method: string, params?: Record<string, unknown>): Promise<T>;
}

/** webClient 的连接态是 idle/connecting/ready/reconnecting/closed，这里映射为三态。 */
export function mapWebClientState(state: string): WebUIConnectionState {
  if (state === 'ready') {
    return 'connected';
  }
  if (state === 'idle') {
    return 'idle';
  }
  return 'disconnected';
}

export function createWebClientTransport(client: WebUIClientLike, capabilities?: ClientCapabilityHost): WebUITransport {
  return {
    subscribe(handlers: WebUITransportHandlers) {
      const accepted = (payload: unknown) => !capabilities || capabilities.acceptsFrame(payload);
      const offRender = client.on(WEBUI_RENDER_EVENT, (event) => { if (accepted(event.payload)) handlers.onRenderFrame(event.payload); });
      const offClear = client.on(WEBUI_CLEAR_EVENT, (event) => { if (accepted(event.payload)) handlers.onClearFrame(event.payload); });
      const offError = client.on(WEBUI_ERROR_EVENT, (event) => { if (accepted(event.payload)) handlers.onErrorFrame(event.payload); });
      // Appearance frames carry no render target; the Gateway refuses calls from a stale activation.
      const offAppearance = client.on(WEBUI_APPEARANCE_EVENT, (event) => handlers.onAppearanceFrame?.(event.payload));
      const offOperation = capabilities ? client.on('client.operation', (event) => { capabilities.acceptOperation(event.payload); }) : undefined;
      const offDeclarations = capabilities ? client.on('client.declarations', (event) => {
        try { capabilities.configure(event.payload); } catch { /* The capability host exposes a localized configuration error. */ }
      }) : undefined;
      const offPackagesChanged = client.on('extensions.v2.installed', (event) => {
        const ids = event.payload.package_ids;
        if (Array.isArray(ids) && ids.length <= 128 && ids.every((id) => typeof id === 'string' && id.length <= 128)) {
          const sessionId = event.payload.session_id;
          handlers.onPackagesChanged?.(ids, typeof sessionId === 'string' ? sessionId : undefined);
        }
      });
      const offState = client.onStateChange((state) => {
        handlers.onConnectionStateChange(mapWebClientState(state));
      });
      return () => {
        offRender();
        offClear();
        offError();
        offAppearance();
        offPackagesChanged();
        offState();
        offOperation?.();
        offDeclarations?.();
      };
    },
    async dispatch(request) {
      try {
        const isApplication = request.target.scope === 'application';
        const response = await client.request<unknown>(WEBUI_DISPATCH_METHOD, {
          event: request.event,
          ...(isApplication ? {} : { session_id: request.target.sessionId }),
          target: {
            ...(isApplication ? { scope: 'application' } : {}),
            slot_id: request.target.slotId,
            package_id: request.target.packageId,
            instance_key: request.target.instanceKey,
          },
          payload: request.payload,
        });
        return toReceipt(response);
      } catch {
        // 请求失败不改变当前界面，也不自动重试；回执为 null 表示结果未知。
        return null;
      }
    },
    getConnectionState() {
      return mapWebClientState(client.getState());
    },
  };
}

function toReceipt(value: unknown): WebUIDispatchReceipt | null {
  if (typeof value !== 'object' || value === null || Array.isArray(value)) {
    return null;
  }
  const record = value as Record<string, unknown>;
  if (typeof record.accepted !== 'boolean' || typeof record.status !== 'string') {
    return null;
  }
  const receipt: WebUIDispatchReceipt = { accepted: record.accepted, status: record.status };
  if (typeof record.code === 'string') {
    receipt.code = record.code;
  }
  return receipt;
}
