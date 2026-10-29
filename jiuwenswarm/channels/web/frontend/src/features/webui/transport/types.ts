/**
 * 接入适配层接口：真实通信与 Mock 遵守同一契约。
 * 该层只负责消息外壳与连接状态，业务数据以 unknown 交给协议处理层。
 */

import type { WebUINormalizedTarget } from '../contracts/normalized.ts';
import type { WebUIUpstreamEvent } from '../contracts/wire.ts';

export type WebUIConnectionState = 'idle' | 'connected' | 'disconnected';

export interface WebUITransportHandlers {
  onRenderFrame: (payload: unknown) => void;
  onClearFrame: (payload: unknown) => void;
  onErrorFrame: (payload: unknown) => void;
  onConnectionStateChange: (state: WebUIConnectionState) => void;
}

export interface WebUIDispatchRequest {
  event: WebUIUpstreamEvent;
  target: WebUINormalizedTarget;
  payload: Record<string, unknown>;
}

export interface WebUIDispatchReceipt {
  accepted: boolean;
  status: string;
  code?: string;
}

export interface WebUITransport {
  subscribe(handlers: WebUITransportHandlers): () => void;
  dispatch(request: WebUIDispatchRequest): Promise<WebUIDispatchReceipt | null>;
  getConnectionState(): WebUIConnectionState;
}
