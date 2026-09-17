/**
 * WebSocket 消息类型
 */

export type WebConnectionState =
  | 'idle'
  | 'connecting'
  | 'ready'
  | 'reconnecting'
  | 'closed';

export interface WsRequest {
  type: 'req';
  id: string;
  method: string;
  params?: Record<string, unknown>;
  is_stream?: boolean;
}

export interface WsResponse {
  type: 'res';
  id: string;
  ok: boolean;
  payload?: unknown;
  error?: string;
  code?: string;
}

export interface WsEvent {
  type: 'event';
  event: string;
  payload: Record<string, unknown>;
  seq?: number;
  stream_id?: string;
}

export type WebMessage = WsRequest | WsResponse | WsEvent;

export interface WebRequestOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  /** 对应协议里请求消息的顶层 is_stream 字段（如 command.goal 的 set/resume） */
  isStream?: boolean;
}

export interface WebConnectOptions {
  provider?: string;
  apiKey?: string;
  apiBase?: string;
  model?: string;
  projectDir?: string;
}

export interface WebError extends Error {
  code?: string;
  requestId?: string;
  retriable?: boolean;
}

export interface ConnectionAckPayload {
  session_id?: string;
  mode?: string;
  tools?: string[];
  protocol_version?: string;
  /** 当前全局是否有任务在跑（后端 ack 推送，用于初始化配置保存锁）。 */
  task_running?: boolean;
}

export interface ProcessingStatusPayload {
  is_processing: boolean;
  current_task?: string;
}

export interface EvolutionStatusPayload {
  session_id?: string;
  status: 'start' | 'progress' | 'end';
  stage?: string;
  message?: string;
}

export interface ErrorPayload {
  error: string;
  code?: string;
  recoverable: boolean;
}

/**
 * 中断意图类型
 */
export type InterruptIntent = 'pause' | 'cancel' | 'supplement' | 'resume';

/**
 * 中断结果 Payload
 */
export interface InterruptResultPayload {
  intent: InterruptIntent;
  success: boolean;
  message: string;
  new_input?: string;
  merged_input?: string;
  paused_task?: string;
  has_active_task?: boolean;  // 是否有活跃任务，false 表示任务已完成
}

/**
 * 子任务状态类型
 */
export type SubtaskStatus = 'starting' | 'tool_call' | 'tool_result' | 'completed' | 'error';

/**
 * 子任务更新 Payload
 */
export interface SubtaskUpdatePayload {
  task_id: string;
  description: string;
  status: SubtaskStatus;
  index: number;
  total: number;
  tool_name?: string;
  tool_count?: number;
  message?: string;
  is_parallel?: boolean;
}

/**
 * 问题选项
 */
export interface QuestionOption {
  label: string;
  description?: string;
  value?: string;
}

/**
 * 问题定义
 */
export interface Question {
  question: string;
  header: string;
  options: QuestionOption[];
  multi_select?: boolean;
}

/**
 * 用户问题请求 Payload（服务端 -> 客户端）
 */
export interface AskUserQuestionPayload {
  request_id: string;
  questions: Question[];
  source?: string; // 来源标识，用于区分自进化确认和工具权限确认
  approvalSchema?: string;
  evolutionMeta?: Record<string, unknown>;
  planApprovalKind?: string;
  planContent?: string;
  planLanguage?: 'cn' | 'en';
}

/**
 * 用户回答
 */
export interface UserAnswer {
  /**
   * 服务端下发的原始问题文本。
   * ask_user_interrupt 场景必填，后端据此把答案归属到对应问题。
   */
  question?: string;
  selected_options: string[];
  custom_input?: string;
}

/**
 * 用户回答 Payload（客户端 -> 服务端）
 */
export interface UserAnswerPayload {
  request_id: string;
  answers: UserAnswer[];
  evolution_meta?: Record<string, unknown>;
  plan_approval_kind?: string;
  plan_content?: string;
  plan_language?: 'cn' | 'en';
}
