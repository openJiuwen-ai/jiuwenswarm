/**
 * 类型导出
 */

export * from './goal';
export * from './message';
export * from './skillTree';
export * from './beamSearch';
export * from './todo';
export * from './websocket';
export * from './subagent';
export * from '../features/workspace/projectTypes';

// 会话类型
export interface Session {
  session_id: string;
  title: string;
  project_id: string;
  project_dir: string;
  /** Session 创建时锁定；true 表示启用 Persist Session，创建后不可修改。 */
  persist_session?: boolean;
  work_mode?: import('../features/workspace/projectTypes').WorkMode;
  pinned?: boolean;
  pin_order?: number;
  renamed_at?: string | null;
  display_title?: string | null;
  is_custom_title?: boolean;
  title_source?: 'auto' | 'user';
  model?: string;
  mode: AgentMode;
  status: SessionStatus;
  message_count: number;
  created_at: string;
  updated_at: string;
  is_active?: boolean;
  is_processing?: boolean;
  current_task?: string;
  tools?: string[];
  team_name?: string;
  // ---- session.list 扩展字段 ----
  channel_id?: string;         // 渠道ID
  user_id?: string;            // 创建人ID
  last_message_at?: number;    // 最近对话时间(Unix时间戳)
  last_user_message_at?: number; // 最后一条用户消息时间(Unix时间戳)
}

export type AgentMode =
  // 旧 UI 基础模式（localStorage 兼容期保留）
  | 'agent'
  | 'team'
  | 'auto_harness'
  // 新三段命名 canonical（与 TUI ClientMode 对齐，前端 normalizeAgentMode 仍归一到基础三态）
  | 'agent.work.normal'
  | 'agent.work.plan'
  | 'agent.code.normal'
  | 'agent.code.plan'
  | 'team.work.normal'
  | 'team.work.plan'
  | 'team.code.normal'
  | 'team.code.plan';
export type SessionStatus = 'active' | 'paused' | 'completed' | 'interrupted';
export type Permission = 'default' | 'full_access';

export type ModelPlan = 'token_plan' | 'coding_plan' | 'custom_api';

export interface ModelEntry {
  model_name: string;
  api_base: string;
  api_key: string;
  model_provider: string;
  timeout?: number;
  temperature?: number;
  reasoning_level?: string;
  context_window_tokens?: number;
  /** 同 model_name 组内的默认勾选标识 */
  is_default?: boolean;
  /** 可选别名，用于快捷切换模型（如 "gpt" → "gpt-4o"） */
  alias?: string;
  /** 用于原子性重命名操作，指定原模型名 */
  original_model_name?: string;
  /**
   * 持久化条目在 models.defaults 中的索引；由 models.list 透传。
   * replace_all 据此识别"未编辑字段"并保留 YAML 占位符（如 ${API_KEY}）。
   * 新增条目不带此字段。
   */
  origin_index?: number;
  /** 服务端厂商预设标识；与 plan 共同定位一次加载周期内的预设。 */
  vendor_key?: string;
  /** 服务端返回的套餐分组。前端只透传，不自行推断或持久化生成。 */
  plan?: ModelPlan;
  /** OpenAI 兼容接口的端点方言；Anthropic 协议不携带此字段。 */
  endpoint_profile?: string;
  /** 免费模型标识（如 Opencode Zen 免费模型）。前端据此归入"免费模型"分组；非免费模型不带此字段。 */
  is_free?: boolean;
  /** AgentOS 备份模型只读标识；此类条目不参与 models.replace_all。 */
  is_agentos?: boolean;
}

export interface VendorPreset {
  vendor_key: string;
  display_name: string;
  plan: ModelPlan;
  client_provider: string;
  api_base: string;
  endpoint_profile?: string | null;
  default_model: string;
  model_options: string[];
  icon_key: string;
  models_endpoint: string | null;
  models_needs_key: boolean;
  supports_anthropic: boolean;
  anthropic_base: string | null;
  anthropic_client_provider: string | null;
}

export type VendorPresetMap = Record<ModelPlan, VendorPreset[]>;

export interface VendorFetchModelsResult {
  models: string[];
  source: 'remote' | 'preset';
  reason?: string;
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
