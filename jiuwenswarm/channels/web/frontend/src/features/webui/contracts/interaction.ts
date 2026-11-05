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
  /** 仅 status 为 sent 时携带；null 表示结果未知。 */
  receipt?: WebUIInteractionReceipt | null;
  /** status 为 failed 时的诊断信息。 */
  error?: string;
}
