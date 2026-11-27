/**
 * 类型导出
 */

export * from './message';
export * from './todo';
export * from './websocket';

// 会话类型
export interface Session {
  session_id: string;
  title: string;
  project_path: string;
  mode: AgentMode;
  status: SessionStatus;
  message_count: number;
  created_at: string;
  updated_at: string;
  is_active?: boolean;
  is_processing?: boolean;
  current_task?: string;
  tools?: string[];
  // ---- session.list 扩展字段 ----
  channel_id?: string;         // 渠道ID
  user_id?: string;            // 创建人ID
  last_message_at?: number;    // 最近对话时间(Unix时间戳)
}

export type AgentMode = 'agent.fast' | 'agent.plan' | 'team';
export type SessionStatus = 'active' | 'paused' | 'completed' | 'interrupted';

export interface ModelEntry {
  model_name: string;
  api_base: string;
  api_key: string;
  model_provider: string;
  timeout?: number;
  temperature?: number;
  /** 用于原子性重命名操作，指定原模型名 */
  original_model_name?: string;
}

export interface OffloadFileListResponse {
  session_id: string;
  files: string[];
  path: string;
  total: number;
}

export interface OffloadFileContentResponse {
  session_id: string;
  filename: string;
  content: string;
  path: string;
}
