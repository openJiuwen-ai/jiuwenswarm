/** 工作空间管理（§2）类型。 */

export type WorkspaceZone =
  | 'session_workspace'
  | 'session_todo'
  | 'session_meta'
  | 'skills'
  | 'role_md'
  | 'memory'
  | 'runtime'
  | 'artifact'
  | 'other';

export type WorkspaceQuotaStatus = 'ok' | 'warn' | 'block';

export interface WorkspaceEntry {
  name: string;
  relative_path: string;
  is_dir: boolean;
  size_bytes: number | null;
  mtime_ms: number;
  ctime_ms: number;
  zone: WorkspaceZone | string;
  deletable: boolean;
  mime_hint: string | null;
}

export interface WorkspaceTreeData {
  relative_path: string;
  entries: WorkspaceEntry[];
}

export interface WorkspaceUsageData {
  user_id?: string;
  group_id?: string;
  bot_id?: string;
  used_bytes: number;
  limit_bytes: number;
  percent: number;
  status: WorkspaceQuotaStatus;
  source_policy_id?: string;
  /** limit_bytes === -1 时为 true；前端优先读此字段 */
  unlimited?: boolean;
}

export interface WorkspaceDeleteResult {
  relative_path: string;
  ok: boolean;
  error: string | null;
}

export interface WorkspaceDeleteData {
  results: WorkspaceDeleteResult[];
}

export interface WorkspacePreviewData {
  relative_path: string;
  truncated: boolean;
  content: string | null;
}

export interface WorkspaceExpandCreateResult {
  order_num: string;
  status: string;
}
