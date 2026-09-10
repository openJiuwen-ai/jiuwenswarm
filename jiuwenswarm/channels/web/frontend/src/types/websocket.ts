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
  /** 后端在 chat.* 事件 payload 内注入的请求关联 id（chat.delta/final 不携带）。 */
  request_id?: string;
}

export type WebMessage = WsRequest | WsResponse | WsEvent;

export interface WebRequestOptions {
  timeoutMs?: number;
  signal?: AbortSignal;
  /** 对应协议里请求消息的顶层 is_stream 字段（如 command.goal 的 set/resume） */
  isStream?: boolean;
  /** 调用方指定请求 id，用于 chat.send/chat.interrupt 的流式事件关联与取消过滤。 */
  requestId?: string;
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
  payload?: unknown;
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
  agent_scope_id?: string; // 子 Agent 委托审批的严格关联标识
  /** Skill 加载审批卡（可选；缺失时回退渲染 message markdown） */
  skill_approval_card?: SkillApprovalCardPayload;
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
  /** Skill 审批卡显式动作（approve_once/approve_session/continue_without_overlay），普通卡片无此字段 */
  action?: string;
}

/** AskUser 交互的显式完成状态。缺失时后端按 answered 处理。 */
export type UserAnswerStatus = 'answered' | 'skipped';

/**
 * 用户回答 Payload（客户端 -> 服务端）
 */
export interface UserAnswerPayload {
  request_id: string;
  status?: UserAnswerStatus;
  answers: UserAnswer[];
  agent_scope_id?: string;
  evolution_meta?: Record<string, unknown>;
  plan_approval_kind?: string;
  plan_content?: string;
  plan_language?: 'cn' | 'en';
}

/**
 * Skill 加载审批动作（与后端 SkillApprovalAction 契约一致）
 */
export type SkillApprovalAction =
  | 'approve_once'
  | 'approve_session'
  | 'continue_without_overlay';

/**
 * Skill 权限差分（放宽项在前、收紧其次、被安全策略丢弃的单列）
 */
export interface SkillApprovalDiff {
  widened: string[];
  tightened: string[];
  rejected: string[];
}

/**
 * Skill 加载审批卡结构化 Payload（对应后端 SkillApprovalCard.to_dict()，
 * 经 interrupt payload_schema["x-skill-approval-card"] 下发）
 */
export interface SkillApprovalCardPayload {
  kind: 'skill_approval';
  schema_version: 1;
  skill_name: string;
  source: string;
  version?: string | null;
  trust: 'builtin' | 'other';
  permissions_hash: string;
  agent_scope_id: string;
  cached_decision?: 'local' | 'session' | null;
  diff: SkillApprovalDiff;
  actions: SkillApprovalAction[];
}
