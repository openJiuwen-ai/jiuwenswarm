export type ReaSetupItemState =
  | 'ready'
  | 'missing'
  | 'config_drift'
  | 'registered'
  | 'connected'
  | 'unsupported';

export interface ReaSetupBaseItem {
  id: string;
  label: string;
  ok: boolean;
  required: boolean;
  state: string;
  detail: string;
  version?: string | null;
  npm_version?: string | null;
  path?: string | null;
  tool_count?: number;
}

export interface ReaSetupEnhancedItem {
  id: string;
  label: string;
  group: 'analysis_engine' | 'specialized_tool' | string;
  detected: boolean;
  available: boolean | null;
  status: string;
  code?: string | null;
  reason?: string | null;
  install_supported: boolean;
  install_mode?: string;
  install_hint?: string;
  default_selected?: boolean;
  env_key?: string;
  assisted_setup?: ReaAssistedSetup;
  configured?: boolean;
  local_state?: 'configured' | 'discovered' | 'cached' | 'partial' | 'missing' | 'unsupported';
  local_path?: string | null;
  local_paths?: string[];
  inventory?: {
    state: string;
    detected: boolean;
    configured: boolean;
    path?: string | null;
    paths?: string[];
    reason?: string;
    source?: string;
    version?: string | null;
    env?: Record<string, string>;
  };
}

export interface ReaSetupPlanItem {
  id: string;
  label: string;
  group: string;
  install_mode: string;
  state: 'ready' | 'planned' | 'needs_action' | 'unsupported';
  reason: string;
  auto_install: boolean;
  platforms?: string[];
  default_selected: boolean;
}

export interface ReaAssistedInput {
  id: string;
  label: string;
  type: 'path' | 'directory' | 'json' | 'text';
  env_key: string;
  target?: string;
  placeholder?: string;
  help?: string;
  required?: boolean;
  expect?: 'executable';
  format?: 'url';
  probe_files?: string[];
}

export interface ReaAssistedCommand {
  label: string;
  content: string;
  language?: 'powershell' | 'shell' | 'text';
  note?: string;
  platforms?: string[];
}

export interface ReaAssistedSetup {
  summary?: string;
  prerequisites?: string[];
  instructions: string[];
  links: Array<{ label: string; url: string }>;
  commands?: ReaAssistedCommand[];
  inputs: ReaAssistedInput[];
  required?: string[];
  required_any?: string[];
  activation?: string;
}

export interface ReaSetupScan {
  ready: boolean;
  platform: string;
  platform_id: string;
  rea_version: string;
  base: ReaSetupBaseItem[];
  enhanced: ReaSetupEnhancedItem[];
  enhanced_plan: ReaSetupPlanItem[];
  rea_skill?: {
    name: string;
    label: string;
    installed: boolean;
    updated: boolean;
    path?: string | null;
    version?: string | null;
    package_version?: string | null;
    error?: string | null;
  };
  capability_cards?: ReaCapabilityCard[];
  rea_inventory_diagnostics?: {
    rea_version?: string;
    expected_rea_version?: string;
    tool_count?: number;
    expected_tool_count?: number;
    group_count?: number;
    expected_group_count?: number;
    availability_tool_count?: number;
    mapped_tool_count?: number;
    core_tool_count?: number;
    unknown_tool_group_count?: number;
    unknown_tool_groups?: Array<{
      surface: string;
      first_tool: string;
      count: number;
      names: string[];
    }>;
    changed_tool_group_count?: number;
    group_topology_variable?: boolean;
    inventory_contract_ok?: boolean;
    inventory_warning?: string;
    error?: string;
  };
  enhanced_blocked_by_base: boolean;
  generated_at: number;
}

export interface ReaCapabilityCard {
  id: string;
  label: string;
  description: string;
  state:
    | 'available'
    | 'partial'
    | 'needs_action'
    | 'unsupported'
    | 'not_installed'
    | 'installing'
    | 'failed';
  ready_members: number;
  total_members: number;
  unsupported_members: number;
  scenario_required: number;
  manual_actions: string[];
  members: Array<{
    id: string;
    label: string;
    kind: 'component' | 'tool_group';
    state: string;
    tool_names?: string[];
  }>;
  tools: Array<{
    name: string;
    available: boolean;
    reason?: string;
    remediation?: string;
    surface?: string;
  }>;
}

export type ReaEnhancedResultState =
  | 'installed'
  | 'reused'
  | 'needs_action'
  | 'unsupported'
  | 'skipped'
  | 'failed';

export interface ReaEnhancedInstallResult {
  id: string;
  label: string;
  state: ReaEnhancedResultState;
  ok: boolean;
  verified: boolean;
  path?: string | null;
  version?: string | null;
  source?: string | null;
  mirror_fallback?: boolean;
  error?: string | null;
  reason?: string | null;
  verification?: {
    ok: boolean;
    code?: string | null;
    detail?: string;
    version?: string | null;
  };
}

export interface ReaSetupInstallResult {
  kind: 'base' | 'enhanced';
  base_ready?: boolean;
  outcome?: 'complete' | 'partial' | 'failed';
  mcp_verified: boolean;
  mcp_tool_count: number;
  mcp_tools: string[];
  verification_error?: string | null;
  enhanced_install_results?: ReaEnhancedInstallResult[];
  enhanced_plan?: ReaSetupPlanItem[];
  summary?: Partial<Record<ReaEnhancedResultState, number>>;
  capability_cards?: ReaCapabilityCard[];
  rea_inventory_diagnostics?: ReaSetupScan['rea_inventory_diagnostics'];
  tool_inventory_verified?: boolean;
  scan: ReaSetupScan;
  previous_ready?: boolean;
}

export interface ReaSetupStep {
  id: string;
  label: string;
  status: 'pending' | 'running' | 'succeeded' | 'failed' | 'skipped';
  expected_seconds_min: number;
  expected_seconds_max: number;
  started_at?: number | null;
  finished_at?: number | null;
  detail?: string | null;
}

export interface ReaSetupProgressDetail {
  component_id?: string;
  component_label?: string;
  phase?: string;
  source?: string;
  downloaded_bytes?: number;
  total_bytes?: number | null;
  component_total_bytes?: number;
  cumulative_downloaded_bytes?: number;
  cumulative_total_bytes?: number;
  bytes_per_second?: number;
  eta_seconds?: number | null;
  source_index?: number;
  source_count?: number;
  mirror_fallback?: boolean;
  component_progress?: number;
  mcp_tool_count?: number;
  component_result?: ReaEnhancedInstallResult;
  error?: string;
}

export interface ReaSetupJob {
  job_id: string;
  kind: 'base' | 'enhanced';
  status: 'queued' | 'running' | 'succeeded' | 'failed' | 'cancelled';
  stage: string;
  percent: number;
  message: string;
  started_at: number;
  updated_at: number;
  steps?: ReaSetupStep[];
  current_step_id?: string | null;
  detail?: ReaSetupProgressDetail;
  component_results?: Record<string, ReaEnhancedInstallResult>;
  estimated_seconds_min?: number;
  estimated_seconds_max?: number;
  plan?: ReaSetupPlanItem[];
  result?: ReaSetupInstallResult | null;
  error?: string | null;
}

export interface ReaComponentConfigureResult {
  configured: boolean;
  mcp_verified: boolean;
  verification_error?: string | null;
  scan: ReaSetupScan;
}
