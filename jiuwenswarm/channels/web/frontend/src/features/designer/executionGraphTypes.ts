export const DESIGNER_GRAPH_SCHEMA_VERSION = 'designer-execution-graph.v1' as const;
export const DESIGNER_RUN_SCHEMA_VERSION = 'designer-execution-run.v1' as const;

export const DESIGNER_NODE_TYPE_TEXT = 'text' as const;
export const DESIGNER_NODE_TYPE_TABLE = 'table' as const;
export const DESIGNER_NODE_TYPE_IMAGE = 'image' as const;
export const DESIGNER_NODE_TYPE_VIDEO = 'video' as const;
export const DESIGNER_NODE_TYPE_AUDIO = 'audio' as const;

export const DESIGNER_NODE_TYPES = [
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_VIDEO,
  DESIGNER_NODE_TYPE_AUDIO,
] as const;

export type DesignerNodeType = (typeof DESIGNER_NODE_TYPES)[number];

export const DESIGNER_NODE_ROLE_BRIEF = 'brief' as const;
export const DESIGNER_NODE_ROLE_CHARACTER_DESIGN = 'character_design' as const;
export const DESIGNER_NODE_ROLE_SCENE = 'scene' as const;
export const DESIGNER_NODE_ROLE_STORYBOARD = 'storyboard' as const;
export const DESIGNER_NODE_ROLE_FRAME = 'frame' as const;
export const DESIGNER_NODE_ROLE_CLIP = 'clip' as const;
export const DESIGNER_NODE_ROLE_COMPOSE = 'compose' as const;
export const DESIGNER_NODE_ROLE_MUSIC = 'music' as const;
export const DESIGNER_NODE_ROLE_SPEECH = 'speech' as const;
export const DESIGNER_NODE_ROLE_TEXT = 'text' as const;
export const DESIGNER_NODE_ROLE_TABLE = 'table' as const;
export const DESIGNER_NODE_ROLE_IMAGE = 'image' as const;
export const DESIGNER_NODE_ROLE_VIDEO = 'video' as const;
export const DESIGNER_NODE_ROLE_AUDIO = 'audio' as const;

export const DESIGNER_NODE_ROLES = [
  DESIGNER_NODE_TYPE_TEXT,
  DESIGNER_NODE_TYPE_TABLE,
  DESIGNER_NODE_TYPE_IMAGE,
  DESIGNER_NODE_TYPE_VIDEO,
  DESIGNER_NODE_TYPE_AUDIO,
] as const;

export type DesignerNodeRole = (typeof DESIGNER_NODE_ROLES)[number];

export const DESIGNER_CONFIG_DELEGATE_HANDLER = 'handler' as const;
export const DESIGNER_CONFIG_DELEGATE_SUBAGENT = 'subagent' as const;
export const DESIGNER_CONFIG_DELEGATE_AGENT = 'agent' as const;

export const DESIGNER_CONFIG_DELEGATES = [
  DESIGNER_CONFIG_DELEGATE_HANDLER,
  DESIGNER_CONFIG_DELEGATE_SUBAGENT,
  DESIGNER_CONFIG_DELEGATE_AGENT,
] as const;

export type DesignerConfigDelegate = (typeof DESIGNER_CONFIG_DELEGATES)[number];

export const DESIGNER_AGENT_GROUP_NAME = 'designer' as const;

export const DESIGNER_ROLE_DEFAULT_TEMPLATES = {
  [DESIGNER_NODE_ROLE_BRIEF]: 'designer/leader',
  [DESIGNER_NODE_ROLE_CHARACTER_DESIGN]: 'designer/character',
  [DESIGNER_NODE_ROLE_SCENE]: 'designer/scene',
  [DESIGNER_NODE_ROLE_STORYBOARD]: 'designer/storyboard',
  [DESIGNER_NODE_ROLE_FRAME]: 'designer/frame',
  [DESIGNER_NODE_ROLE_CLIP]: 'designer/clip',
  [DESIGNER_NODE_ROLE_COMPOSE]: 'designer/clip',
  [DESIGNER_NODE_TYPE_TEXT]: 'designer/leader',
  [DESIGNER_NODE_TYPE_TABLE]: 'designer/storyboard',
  [DESIGNER_NODE_TYPE_IMAGE]: 'designer/character',
  [DESIGNER_NODE_TYPE_VIDEO]: 'designer/clip',
} as const;

export const DESIGNER_NODE_CONFIG_KEYS = [
  'role',
  'pipeline',
  'prompt',
  'inputs',
  'delegate',
  'agent_template',
  'collaborate',
  'generate',
  'upload',
  'edit',
  'interaction_mode',
  'materials',
] as const;

export type DesignerMediaGenerateConfig = {
  prompt?: string;
  prompt_origin?: 'storyboard' | 'user';
  aspect_ratio?: string;
  resolution?: string;
  duration?: string;
  has_audio?: boolean;
  count?: number;
};

export type DesignerMediaUploadConfig = {
  filename?: string;
  asset_id?: string;
  mime_type?: string;
  uri?: string;
};

export type DesignerMediaEditConfig = {
  content?: string;
};

export type DesignerMediaMaterialSlot = {
  id: string;
  label?: string;
  filename?: string;
  mime_type?: string;
  asset_id?: string;
  uri?: string;
};

export type DesignerComfyuiClassType = 'VLLMOmniGenerateImage' | 'VLLMOmniGenerateVideo';

/** Own widgets of the ComfyUI generate node; fps / duration exist on video only. */
export type DesignerComfyuiFields = {
  url: string;
  model: string;
  negative_prompt: string;
  width: number;
  height: number;
  fps?: number;
  duration?: number;
};

/** VLLMOmniDiffusionSampling minus ``n`` (one canvas node is one output). */
export type DesignerComfyuiSamplingParams = {
  num_inference_steps: number;
  guidance_scale: number;
  true_cfg_scale: number;
  vae_use_slicing: boolean;
  vae_use_tiling: boolean;
  /** -1 lets the server pick. */
  seed: number;
};

export type DesignerComfyuiModelParams =
  | { type: 'minimax_h3'; audio_flow_shift?: number; flow_shift?: number }
  | { type: 'wan'; guidance_scale_2?: number; boundary_ratio?: number; flow_shift?: number };

export type DesignerComfyuiConfig = {
  class_type: DesignerComfyuiClassType;
  source_node_id: string;
  fields: DesignerComfyuiFields;
  sampling_params: DesignerComfyuiSamplingParams | null;
  model_params: DesignerComfyuiModelParams | null;
};

type DesignerRoleConfig<R extends DesignerNodeRole | string> = {
  role: R;
  pipeline?: string;
  prompt?: string;
  inputs?: string[];
  delegate?: DesignerConfigDelegate;
  agent_template?: string;
  collaborate?: boolean;
  generate?: DesignerMediaGenerateConfig;
  upload?: DesignerMediaUploadConfig;
  edit?: DesignerMediaEditConfig;
  interaction_mode?: 'generate' | 'upload' | 'edit';
  materials?: DesignerMediaMaterialSlot[];
  shot_index?: number;
  /** Canvas-added by the user (Director onboards as LLM agent when available). */
  user_added?: boolean;
  /** User dragged the node resize handle; skip content auto-fit. */
  user_resized?: boolean;
  kind?: string;
  /** Imported from a ComfyUI workflow; never shown, always generates via vLLM-Omni. */
  is_comfyui?: boolean;
  comfyui?: DesignerComfyuiConfig;
  force_handler?: boolean;
  skip_llm?: boolean;
  /** The upload replaced the generated output. */
  user_replaced_output?: boolean;
};

export type DesignerNodeConfig =
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_BRIEF>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_CHARACTER_DESIGN>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_SCENE>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_STORYBOARD>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_FRAME>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_CLIP>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_COMPOSE>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_MUSIC>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_SPEECH>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_IMAGE>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_VIDEO>
  | DesignerRoleConfig<typeof DESIGNER_NODE_ROLE_AUDIO>
  | {
      role?: string;
      pipeline?: string;
      prompt?: string;
      inputs?: string[];
      delegate?: string;
      agent_template?: string;
      collaborate?: boolean;
      generate?: DesignerMediaGenerateConfig;
      upload?: DesignerMediaUploadConfig;
      edit?: DesignerMediaEditConfig;
      interaction_mode?: string;
      materials?: DesignerMediaMaterialSlot[];
      shot_index?: number;
      user_added?: boolean;
      user_resized?: boolean;
      kind?: string;
      is_comfyui?: boolean;
      comfyui?: DesignerComfyuiConfig;
      force_handler?: boolean;
      skip_llm?: boolean;
      user_replaced_output?: boolean;
    };

export const DESIGNER_EDGE_KIND_DATA = 'data' as const;
export const DESIGNER_EDGE_KIND_SYNC = 'sync' as const;

export const DESIGNER_EDGE_KINDS = [DESIGNER_EDGE_KIND_DATA, DESIGNER_EDGE_KIND_SYNC] as const;

export type DesignerEdgeKind = (typeof DESIGNER_EDGE_KINDS)[number];

export const DESIGNER_GRAPH_SOURCE_PROMPT = 'prompt' as const;
export const DESIGNER_GRAPH_SOURCE_MANUAL = 'manual' as const;

export const DESIGNER_NODE_STATUS_PENDING = 'pending' as const;
export const DESIGNER_NODE_STATUS_RUNNING = 'running' as const;
export const DESIGNER_NODE_STATUS_COMPLETED = 'completed' as const;
export const DESIGNER_NODE_STATUS_FAILED = 'failed' as const;
export const DESIGNER_NODE_STATUS_CANCELLED = 'cancelled' as const;

export const DESIGNER_LEADER_NODE_ID = '__leader__' as const;
export const DESIGNER_ACTIVITY_KIND_THINKING = 'thinking' as const;
export const DESIGNER_ACTIVITY_KIND_TOOL_CALL = 'tool_call' as const;
export const DESIGNER_ACTIVITY_KIND_STAGE = 'stage' as const;
export const DESIGNER_ACTIVITY_KINDS = [
  DESIGNER_ACTIVITY_KIND_THINKING,
  DESIGNER_ACTIVITY_KIND_TOOL_CALL,
  DESIGNER_ACTIVITY_KIND_STAGE,
] as const;

export type DesignerActivityKind = (typeof DESIGNER_ACTIVITY_KINDS)[number];

export type DesignerNodeActivity = {
  kind: DesignerActivityKind | string;
  text: string;
  tool?: string;
  at?: number;
};

export const DESIGNER_RUN_STATUS_DRAFT = 'draft' as const;
export const DESIGNER_RUN_STATUS_RUNNING = 'running' as const;
export const DESIGNER_RUN_STATUS_PAUSED = 'paused' as const;
export const DESIGNER_RUN_STATUS_COMPLETED = 'completed' as const;
export const DESIGNER_RUN_STATUS_FAILED = 'failed' as const;
export const DESIGNER_RUN_STATUS_CANCELLED = 'cancelled' as const;

export type AssetRef = {
  kind: string;
  uri: string;
  mime_type?: string;
  label?: string;
};

export type NodeLayout = {
  x?: number;
  y?: number;
  width?: number;
  height?: number;
};

export type DesignerGraphNode = {
  id: string;
  type: DesignerNodeType | string;
  label: string;
  config?: DesignerNodeConfig;
  layout?: NodeLayout;
  output_ref?: AssetRef | null;
};

export type DesignerGraphEdge = {
  id: string;
  source: string;
  target: string;
  kind?: DesignerEdgeKind | string;
  label?: string;
};

export type DesignerGraphPatch = {
  title?: string;
  description?: string;
  upsert_nodes?: DesignerGraphNode[];
  upsert_edges?: DesignerGraphEdge[];
  remove_node_ids?: string[];
  remove_edge_ids?: string[];
};

export type DesignerExecutionGraph = {
  schema_version: typeof DESIGNER_GRAPH_SCHEMA_VERSION | string;
  graph_id: string;
  project_id: string;
  title: string;
  description?: string;
  source?: typeof DESIGNER_GRAPH_SOURCE_PROMPT | typeof DESIGNER_GRAPH_SOURCE_MANUAL | string;
  nodes: DesignerGraphNode[];
  edges: DesignerGraphEdge[];
  metadata?: Record<string, unknown>;
  created_at?: number;
  updated_at?: number;
};

export type DesignerNodeState = {
  status: string;
  started_at?: number | null;
  completed_at?: number | null;
  output_ref?: AssetRef | null;
  output_refs?: AssetRef[] | null;
  candidate_output_ref?: AssetRef | null;
  candidate_output_refs?: AssetRef[] | null;
  error?: string | null;
  blocked_by?: string[];
  activity?: DesignerNodeActivity | null;
  activity_tail?: string[] | null;
  activity_log?: DesignerNodeActivity[] | null;
};

export type DesignerExecutionRun = {
  schema_version: typeof DESIGNER_RUN_SCHEMA_VERSION | string;
  run_id: string;
  graph_id: string;
  project_id: string;
  status: string;
  node_states: Record<string, DesignerNodeState>;
  current_node_ids: string[];
  created_at?: number;
  updated_at?: number;
  error?: string | null;
  warning?: string | null;
  warnings?: string[] | null;
  /** `scope_node_ids`: the run only drives these nodes (a ComfyUI generate).
   * `target_node_id`: a single-node generate; Continue leaves this scope explicitly. */
  metadata?: { scope_node_ids?: string[]; target_node_id?: string } & Record<string, unknown>;
};

export type DesignerGraphSummary = {
  graph_id: string;
  project_id: string;
  title: string;
  updated_at?: number;
  run_id?: string | null;
  run_status?: string | null;
  has_video: boolean;
  clip_label?: string | null;
};

export type DesignerGraphBootstrapResult = {
  graph: DesignerExecutionGraph;
  project_id: string;
  project?: {
    project_id: string;
    project_dir: string;
    restored: boolean;
    work_mode: string;
  };
};
