/**
 * SkillPanel 类型定义
 *
 * 从 index.tsx 抽取，内容保持不变。
 */

export interface FileTreeNode {
  name: string;
  path: string;
  type: 'file' | 'directory';
  size: number | null;
  mime_type: string | null;
  children: FileTreeNode[];
}

export type SkillItem = {
  name: string;
  /** 展示名（保留安装来源的原始大小写，如 ClawHub 的 Weather）；缺省回退到 name */
  display_name?: string;
  description: string;
  source: string;
  version: string;
  author: string;
  tags: string[];
  allowed_tools: string[];
  marketplace?: string;
  /** SkillNet 等安装来源 URL，与在线搜索 skill_url 对照「已安装」 */
  origin?: string;
  /** 是否为内置技能（不允许删除） */
  is_builtin?: boolean;
  /** 是否为内置技能的来源（源码中存在内置版本） */
  is_builtin_source?: boolean;
  /** 本地技能目录是否存在 evolutions.json */
  has_evolutions?: boolean;
  /** 是否启用 */
  enabled?: boolean;
  /** 是否已安装 */
  installed?: boolean;
  /** 技能文件路径（列表去重 / React key） */
  path?: string;
  /** 技能类型：skill | swarm_skill | multimodal_skill */
  skill_type?: string;
  /** 是否已发布到 SkillHub */
  published?: boolean;
};

export type InstalledPluginItem = {
  plugin_name: string;
  marketplace: string;
  spec: string;
  version: string;
  installed_at: string;
  git_commit?: string | null;
  skills: (string | { name: string; version?: string | null })[];
};

export type SkillDetail = SkillItem & {
  content: string;
  file_path: string;
};

export type HubSkillDetailData = {
  short_desc?: string | null;
  detail_desc?: string | null;
};

export type HubSkillDetail = {
  success: boolean;
  asset_id: string;
  version: string;
  data?: HubSkillDetailData | null;
};

export type SkillVersion = {
  version: string;
  is_default: boolean;
  source: string;
  available: boolean;
  created_at: string;
  updated_at: string;
};

export type SkillVersionsListResponse = {
  success: boolean;
  name: string;
  default_version: string | null;
  versions: SkillVersion[];
};

export type SkillFileEntry = {
  path: string;
  type: 'file' | 'directory';
  size: number | null;
  mime_type: string | null;
};

export type SkillFilesListResponse = {
  name: string;
  files: SkillFileEntry[];
};

export type SkillFilePreview = {
  name: string;
  path: string;
  type: 'file';
  mime_type: string;
  size: number;
  encoding?: string;
  content?: string;
  download_url?: string;
};

export type SkillRebuildResponse = {
  success: boolean;
  result_type: 'followup';
  action: string;
  followup_prompt: string;
  skill_name: string;
  rebuild_target: {
    version: string | null;
    is_default: boolean;
    skill_dir: string;
    content_root: string | null;
    swap_workspace: boolean;
  };
};

export type EvolutionChange = {
  section?: string;
  action?: string;
  content: string;
  target?: string;
};

export type EvolutionEntry = {
  id: string;
  source?: string;
  timestamp?: string;
  context?: string;
  change: EvolutionChange;
  applied?: boolean;
};

export type EvolutionGetResponse = {
  exists: boolean;
  valid?: boolean;
  detail?: string;
  entries?: EvolutionEntry[];
};

export type LoadState = 'idle' | 'loading' | 'success' | 'error';

export type MarketplacePluginItem = {
  asset_id: string;
  name: string;
  display_name?: string | null;
  short_desc?: string | null;
  detail_desc?: string | null;
  icon_uri?: string | null;
  publisher_name: string;
  tags?: string[] | null;
  plugin_type?: string | null;
  category_id?: string | null;
  category_name?: string | null;
  latest_version?: string | null;
  install_count: number;
  like_count: number;
  view_count: number;
  moderation_status?: string | null;
  // 搜索结果额外字段
  source?: string | null;
  identifier?: string | null;
  owner_handle?: string | null;
  native_score?: number | null;
  category?: string | null;
  updated_at?: number | null;
  exact_match?: boolean;
};

export interface SkillPanelProps {
  sessionId: string;
  isConnected: boolean;
  symphonyEnabled: boolean;
  onSymphonyEnabledChange: (enabled: boolean) => Promise<boolean>;
  onNavigateToSettings?: () => void;
  /** 当前是否处于激活状态（左边栏选中技能） */
  isActive?: boolean;
}
