/**
 * Mock 接入适配器：下行帧注入、上行请求记录、连接状态模拟。
 * 交互后的更新仍应作为新的下行帧注入，避免本地验证绕过协议处理链。
 */

import type {
  WebUIConnectionState,
  WebUIDispatchReceipt,
  WebUIDispatchRequest,
  WebUITransport,
  WebUITransportHandlers,
} from './types.ts';

export interface MockTransportOptions {
  connectionState?: WebUIConnectionState;
  /** 默认 true：dispatch 立即返回 accepted；false 时挂起，由测试手动 resolve。 */
  autoAccept?: boolean;
}

export interface MockWebUITransport extends WebUITransport {
  readonly dispatches: readonly WebUIDispatchRequest[];
  readonly pendingDispatchCount: number;
  injectRenderFrame(payload: unknown): void;
  injectClearFrame(payload: unknown): void;
  injectErrorFrame(payload: unknown): void;
  setConnectionState(state: WebUIConnectionState): void;
  resolveNextDispatch(receipt: WebUIDispatchReceipt | null): void;
}

export function createMockTransport(options: MockTransportOptions = {}): MockWebUITransport {
  let connectionState = options.connectionState ?? 'connected';
  const autoAccept = options.autoAccept !== false;
  const handlers = new Set<WebUITransportHandlers>();
  const dispatches: WebUIDispatchRequest[] = [];
  const pending = new Map<number, (receipt: WebUIDispatchReceipt | null) => void>();
  let pendingSeq = 0;

  const broadcast = (deliver: (handler: WebUITransportHandlers) => void): void => {
    for (const handler of handlers) {
      deliver(handler);
    }
  };

  return {
    subscribe(target) {
      handlers.add(target);
      return () => {
        handlers.delete(target);
      };
    },
    async dispatch(request) {
      dispatches.push(request);
      if (autoAccept) {
        return { accepted: true, status: 'handled' };
      }
      pendingSeq += 1;
      const key = pendingSeq;
      return await new Promise<WebUIDispatchReceipt | null>((resolve) => {
        pending.set(key, resolve);
      });
    },
    getConnectionState: () => connectionState,
    get dispatches() {
      return dispatches;
    },
    get pendingDispatchCount() {
      return pending.size;
    },
    injectRenderFrame(payload) {
      broadcast((handler) => handler.onRenderFrame(payload));
    },
    injectClearFrame(payload) {
      broadcast((handler) => handler.onClearFrame(payload));
    },
    injectErrorFrame(payload) {
      broadcast((handler) => handler.onErrorFrame(payload));
    },
    setConnectionState(state) {
      if (state === connectionState) {
        return;
      }
      connectionState = state;
      broadcast((handler) => handler.onConnectionStateChange(state));
    },
    resolveNextDispatch(receipt) {
      const next = pending.entries().next();
      if (next.done) {
        return;
      }
      const [key, resolve] = next.value;
      pending.delete(key);
      resolve(receipt);
    },
  };
}
