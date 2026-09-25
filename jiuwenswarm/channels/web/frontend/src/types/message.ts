/**
 * 消息类型定义
 */

import type { SkillTreePath } from './skillTree';
import type { BeamSearchProgress } from './beamSearch';
import type { HeartbeatAutomationMetadata } from './heartbeat';
import type { CrossSessionMessageMetadata } from '../utils/crossSessionMessage';

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

export interface OutputOrder {
  requestId: string;
  sequence: number;
}

export interface MediaItem {
  type: 'image' | 'audio' | 'video' | 'document';
  mimeType: string;
  mime_type?: string;
  filename: string;
  base64Data?: string;
  base64_data?: string;
  url?: string;
  path?: string;
  sizeBytes?: number;
  size_bytes?: number;
}

export interface UsageSummary {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  input_cost?: number;
  output_cost?: number;
  total_cost?: number;
  cost_source?: string;
  currency?: string | null;
}

export interface FileDownloadItem {
  name: string;
  size: number;
  mime_type: string;
  download_url: string;
  download_token: string;
  /** 工作区绝对/相对路径；用于去重身份（优先于 downloadUrl 中的 exp token） */
  path?: string;
  /** send_file 侧探测：是否为可一键保存的 Skill 包（含 SKILL.md 的 zip/.skill） */
  is_skill_package?: boolean;
}

export type AutoReviewerStatus =
  | 'in_progress'
  | 'approved'
  | 'deterministic_allow'
  | 'manual'
  | 'denied'
  | 'blocked'
  | 'fallback'
  | 'host_revalidation_failed'
  | 'timed_out'
  | 'aborted';

export interface AutoReviewerMetadata {
  reviewer_status?: AutoReviewerStatus;
  final_reviewer_status?: AutoReviewerStatus;
  decision_source?: string;
  risk_level?: string;
  evidence_summary?: string;
  manual_reason_summary?: string;
  user_review_hint?: string;
}

export interface ContextCompressionRuntime {
  status: 'running' | 'completed' | 'unchanged' | 'failed';
  summary: string;
  operationId: string;
  phase?: string;
  processor?: string;
}

export interface ContextCompressionSummary {
  count: number;
  summaries: string[];
}

export interface TeamMemberContextCompressionState {
  runtime?: ContextCompressionRuntime;
  summary?: ContextCompressionSummary;
}

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: string;
  /** A displayed supplement belongs to the existing execution, not a new user turn. */
  supplementalInput?: {
    executionId: string;
    requestId?: string;
    streamMessageId?: string;
    streamOffset: number;
  };
  outputPhaseId?: string;
  outputOrder?: OutputOrder;
  /** Full answer delivered by a delegated agent, distinct from spoken replies. */
  presentation?: 'tool_result';
  /** User-facing conversation output that must remain outside collapsed work. */
  keepExpanded?: boolean;
  /**
   * 流式收尾 / chat.final 完成时刻。不参与时间线排序（排序仍用 timestamp，避免与 goal 卡抢序），
   * 仅作为「任务用时」终点，避免 live 一直停在首包 delta 时间、刷新后变成 final 落盘时间。
   */
  completedAt?: string;
  /** 前端渲染身份，避免业务 id 重复或历史 prepend 导致 React key 抖动 */
  renderKey?: string;
  /** 仅用于大历史渐进发布；实时消息没有该标记。 */
  historyBatchSeq?: number;
  /** Fork 后从直接父会话继承的历史消息；用于定位分支开始边界。 */
  forkedFromSessionId?: string;
  audioBase64?: string;
  audioMime?: string;
  mediaItems?: MediaItem[];
  fileItems?: FileDownloadItem[];
  // 工具调用相关
  toolCall?: ToolCall;
  toolResult?: ToolResult;
  // 是否正在流式输出
  isStreaming?: boolean;
  /** 未收到工具/final 分段边界的集群输出；暂停只关闭光标，不移除此关联。 */
  teamStream?: { requestId?: string };
  usageSummary?: UsageSummary;
  // Harness message flag for special styling
  isHarnessMessage?: boolean;
  // 用户消息附带的技能列表（输入栏选中并发送）
  skills?: string[];
  // 主动推荐消息标记
  isProactiveRecommendation?: boolean;
  proactiveType?: 'skill_recommend' | 'task_reminder' | 'need_exploration';
  /** Web 单 Agent 回复产生时显式选中的专家；历史恢复不能依赖当前选择状态。 */
  agentTemplateName?: string;
  proactiveRecId?: string;  // 推荐唯一ID，用于反馈关联
  proactiveTarget?: string;  // 推荐目标（skill名/待办/探索方向），点赞请求带回后端兜底
  /**
   * 这条用户消息是否曾经用于设置/修改持续目标（"设为目标"徽章）。发送那一刻本地回显消息
   * 直接置 true；历史消息刷新后重新加载时，优先读后端 history 字段
   * `is_goal_objective_message`，没有时再靠 goalStore 持久化的 objective 文本列表按
   * content 回填（见 useWebSocket.ts stampGoalObjectiveMessages）——不能靠实时比对
   * "当前 Goal 的 objective"，目标被清除/替换后旧消息也该继续保留这个标记，这是消息自身的
   * 历史事实，不是当前 Goal 状态的派生值。
   */
  isGoalObjectiveMessage?: boolean;
  isCommandOutput?: boolean;
  /**
   * 该用户消息被 before_chat_request 钩子改写（如敏感内容替换），由
   * chat.message_updated 事件原地替换内容时置位（issue #2792）。气泡可据此
   * 渲染「已按安全策略改写」之类的角标；刷新后从历史加载的消息无此标记。
   */
  hookRewritten?: boolean;
  /** 斜杠命令结果的结构化元数据；避免渲染层依赖 content 的换行分隔。 */
  commandName?: string;
  commandInput?: string;
  commandOutput?: string;
  /**
   * Heartbeat 自动轮的身份标记。后端在自动触发时把 metadata.automation 随实时事件 payload
   * 下发，并随 user/assistant 历史落盘。前端按 run_id 为每轮建独立 user/assistant/error 消息
   * （见 heartbeatAutomation.ts），避免覆盖上一条普通回答。刷新/切会话后历史恢复也读同一个
   * 字段重新盖章，保证实时与历史共用同一识别逻辑。对齐「心跳任务前端开发与接口规格说明2」§7-§9。
   */
  automation?: HeartbeatAutomationMetadata;
  /** 来自同一用户其他会话中 Agent 的后台请求。 */
  crossSession?: CrossSessionMessageMetadata;
}

/** Selected queued message sent by the existing non-interrupting send button. */
export interface ChatSendOptions {
  queuedTaskId: string;
}

export interface MessageForkPoint {
  messageId: string;
  role: MessageRole;
  content: string;
  timestamp: string;
}

export interface ToolCall {
  outputOrder?: OutputOrder;
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  description?: string; // 操作描述，如 "创建 3 个任务"
  formatted_args?: string; // 格式化参数摘要
  /** 模型生成的自然语言目标，原样展示，不走 i18n */
  call_goal?: string;
  /** @deprecated 仅用于兼容旧事件，不参与标题渲染 */
  display_name?: string;
  memberName?: string;
  reviewer?: AutoReviewerMetadata;
}

export interface ToolResult {
  toolName: string;
  result: string;
  success: boolean;
  toolCallId?: string;
  summary?: string;  // 结果摘要
  /** 后台任务已接受但仍在运行，不应被渲染为成功或失败终态。 */
  pending?: boolean;
  /** 历史/实时结果显式标记为超时（与 success=false 一起用于展示「执行失败」） */
  timedOut?: boolean;
  // agentic search（symphony 技能检索）下发的技能树路径，用于内联回放路径流转
  skillTree?: SkillTreePath;
  beamSearch?: BeamSearchProgress;
  /** 仅 symphony_compose_graph 的合法 planned_graph Mermaid 展示投影。 */
  mermaid?: string;
  reviewer?: AutoReviewerMetadata;
}

export type ToolExecutionStatus = 'pending' | 'timeout' | 'completed' | 'error';

export interface ToolExecution {
  outputOrder?: OutputOrder;
  toolCallId: string;
  toolCall: ToolCall;
  result?: ToolResult;
  status: ToolExecutionStatus;
  startedAt: string;
  updatedAt: string;
  timeoutAt: string;
  timedOutAt?: string;
  resultArrivedAfterTimeout?: boolean;
  requestId?: string;
  /** Web 单 Agent 工具调用所属的专家；Team 工具不设置。 */
  agentTemplateName?: string;
  /** 仅用于大历史渐进发布；实时工具没有该标记。 */
  historyBatchSeq?: number;
}

export interface Conversation {
  id: string;
  messages: Message[];
  createdAt: string;
  updatedAt: string;
}
