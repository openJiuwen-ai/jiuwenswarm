/**
 * 渲染层与挂载层共用的交互契约。
 * 适配器不依赖 Runtime：dispatch 返回统一结果，调用方据此展示或记录，不据此修改渲染树。
 */

export type WebUIInteractionStatus =
  | 'sent'
  | 'duplicate'
  | 'disconnected'
  | 'inactive'
  | 'invalid_action'
  | 'unsupported_action'
  | 'failed';

export interface WebUIInteractionReceipt {
  accepted: boolean;
  status: string;
  code?: string;
}

/** 一次交互意图；nodeKey 与 actionId 必须来自当前已渲染的树。 */
export interface WebUIInteractionIntent {
  nodeKey: string;
  actionId: string;
  args?: Record<string, unknown>;
  /** 输入控件等由适配器补充的字段；action_id 与 args 由挂载层统一写入。 */
  payload?: Record<string, unknown>;
}

export interface WebUIInteractionResult {
  status: WebUIInteractionStatus;
  /** 仅 status 为 sent 时携带；null 表示结果未知。 */
  receipt?: WebUIInteractionReceipt | null;
  /** status 为 failed 时的诊断信息。 */
  error?: string;
}
