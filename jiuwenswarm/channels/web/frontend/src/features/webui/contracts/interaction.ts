/**
 * 渲染层与挂载层共用的交互契约。
 * 适配器不依赖 Runtime：emit 返回统一结果，调用方据此展示或记录，不据此修改渲染树。
 */

export type WebUIInteractionStatus =
  | 'sent'
  | 'disconnected'
  | 'inactive'
  | 'invalid_action'
  | 'failed'
  | 'stale';

export interface WebUIInteractionReceipt {
  accepted: boolean;
  status: string;
  code?: string;
}

/** 一次交互意图；nodeKey、event 与节点绑定必须来自当前已渲染的树。 */
export interface WebUIInteractionIntent {
  nodeKey: string;
  event: string;
  data: Record<string, unknown>;
  nodeLeaseId: number;
}

export interface WebUIInteractionResult {
  status: WebUIInteractionStatus;
  /** status 为 sent 或 stale 时可携带；null 表示结果未知。 */
  receipt?: WebUIInteractionReceipt | null;
  /** status 为 failed 时的诊断信息。 */
  error?: string;
}

/**
 * 判断一次组件交互是否失败或被服务端拒绝。
 * 当新渲染帧先于回执到达时，status 为 stale 但 receipt 仍反映服务端处理结果：
 * 仅在明确收到 accepted === true 时视为成功；回执为 null/缺失（结果未知）或 accepted === false 均视为失败。
 */
export function isWebUIInteractionFailed(result: Pick<WebUIInteractionResult, 'status' | 'receipt'>): boolean {
  if (result.status === 'sent' || result.status === 'stale') {
    return !result.receipt?.accepted;
  }
  return true;
}
