/**
 * 消息类型定义
 */

import type { SkillTreePath } from './skillTree';
import type { BeamSearchProgress } from './beamSearch';

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

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
}

export interface FileDownloadItem {
  name: string;
  size: number;
  mime_type: string;
  download_url: string;
  download_token: string;
  /** 工作区绝对/相对路径；用于去重身份（优先于 downloadUrl 中的 exp token） */
  path?: string;
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
  /**
   * 流式收尾 / chat.final 完成时刻。不参与时间线排序（排序仍用 timestamp，避免与 goal 卡抢序），
   * 仅作为「任务用时」终点，避免 live 一直停在首包 delta 时间、刷新后变成 final 落盘时间。
   */
  completedAt?: string;
  /** 前端渲染身份，避免业务 id 重复或历史 prepend 导致 React key 抖动 */
  renderKey?: string;
  audioBase64?: string;
  audioMime?: string;
  mediaItems?: MediaItem[];
  fileItems?: FileDownloadItem[];
  // 工具调用相关
  toolCall?: ToolCall;
  toolResult?: ToolResult;
  // 是否正在流式输出
  isStreaming?: boolean;
  usageSummary?: UsageSummary;
  // Harness message flag for special styling
  isHarnessMessage?: boolean;
  // 用户消息附带的技能列表（输入栏选中并发送）
  skills?: string[];
  // 主动推荐消息标记
  isProactiveRecommendation?: boolean;
  proactiveType?: 'skill_recommend' | 'task_reminder' | 'need_exploration';
  /**
   * 这条用户消息是否曾经用于设置/修改持续目标（"设为目标"徽章）。发送那一刻本地回显消息
   * 直接置 true；历史消息刷新后重新加载时，优先读后端 history 字段
   * `is_goal_objective_message`，没有时再靠 goalStore 持久化的 objective 文本列表按
   * content 回填（见 useWebSocket.ts stampGoalObjectiveMessages）——不能靠实时比对
   * "当前 Goal 的 objective"，目标被清除/替换后旧消息也该继续保留这个标记，这是消息自身的
   * 历史事实，不是当前 Goal 状态的派生值。
   */
  isGoalObjectiveMessage?: boolean;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  description?: string;  // 操作描述，如 "创建 3 个任务"
  formatted_args?: string;  // 格式化参数摘要
  display_name?: string;  // 后端下发的可读展示名，前端优先直接展示
  memberName?: string;
}

export interface ToolResult {
  toolName: string;
  result: string;
  success: boolean;
  toolCallId?: string;
  summary?: string;  // 结果摘要
  /** 历史/实时结果显式标记为超时（与 success=false 一起用于展示「执行失败」） */
  timedOut?: boolean;
  // agentic search（symphony 技能检索）下发的技能树路径，用于内联回放路径流转
  skillTree?: SkillTreePath;
  beamSearch?: BeamSearchProgress;
}

export type ToolExecutionStatus = 'pending' | 'timeout' | 'completed' | 'error';

export interface ToolExecution {
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
}

export interface Conversation {
  id: string;
  messages: Message[];
  createdAt: string;
  updatedAt: string;
}
