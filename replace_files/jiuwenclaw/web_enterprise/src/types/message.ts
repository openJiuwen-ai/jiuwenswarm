/**
 * 消息类型定义
 */

export type MessageRole = 'user' | 'assistant' | 'system' | 'tool';

export interface MediaItem {
  type: 'image' | 'audio' | 'video' | 'document';
  mimeType: string;
  filename: string;
  base64Data?: string;
  url?: string;
}

export interface UsageSummary {
  input_tokens: number;
  output_tokens: number;
  total_tokens: number;
  input_cost?: number;
  output_cost?: number;
  total_cost?: number;
}

export interface Message {
  id: string;
  role: MessageRole;
  content: string;
  timestamp: string;
  audioBase64?: string;
  audioMime?: string;
  mediaItems?: MediaItem[];
  // 工具调用相关
  toolCall?: ToolCall;
  toolResult?: ToolResult;
  // 是否正在流式输出
  isStreaming?: boolean;
  usageSummary?: UsageSummary;
}

export interface ToolCall {
  id: string;
  name: string;
  arguments: Record<string, unknown>;
  description?: string;  // 操作描述，如 "创建 3 个任务"
  formatted_args?: string;  // 格式化参数摘要
  // Team 模式下发起该调用的成员身份（主 agent 为空）
  memberId?: string;
  memberName?: string;
}

export interface ToolResult {
  toolName: string;
  result: string;
  success: boolean;
  toolCallId?: string;
  summary?: string;  // 结果摘要
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
}

export interface Conversation {
  id: string;
  messages: Message[];
  createdAt: string;
  updatedAt: string;
}