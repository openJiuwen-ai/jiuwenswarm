// RSI 优化平台前端类型定义
// 对齐当前 AgentServer RSI 实现的公开响应形状。
// 注意：后端未实现 v0.2.3 契约中的部分列表/报告字段，这些字段在此显式可选。

// §3.1 场景标识
export type RsiScenario = 'HARNESS' | 'ARTIFACT';
// 产物优化子类型（仅 scenario=artifact 时有意义）
export type RsiArtifactType = 'PAPER' | 'PROGRAM';

// §3.3 任务状态枚举
export type RsiTaskStatus = 'CREATED' | 'QUEUED' | 'RUNNING' | 'COMPLETED' | 'FAILED' | 'PAUSED' | 'TERMINATED';

// §3.3 演进树节点类型枚举（全场景统一 5 值）
export type RsiNodeType = 'ROOT' | 'ADOPTED' | 'REJECTED' | 'PROVISIONAL' | 'PRUNED';

// §3.4 usage / token 统一结构
export interface RsiUsage {
  tokens: {
    input: number;
    output: number;
    cache_hit: number;
  };
  cost_estimate: number;
  call_count: number;
}

// §5.1 rsi.dataset.validate
export interface RsiDatasetValidateParams {
  input_file: string;
  scenario: RsiScenario;
  artifact_type?: RsiArtifactType;
}
export interface RsiDatasetValidateResult {
  valid: boolean;
  sample_count: number | null;
  errors: Array<{ reason: string; code: string }>;
}

// §6.1 rsi.task.create
interface RsiTaskCreateBase {
  name: string;
  max_iterations?: number;
  optimization_instruction?: string;
  /** Task-scoped proxy used by paper literature search/fetch/download. */
  web_proxy?: string;
}

export interface RsiHarnessTaskCreateParams extends RsiTaskCreateBase {
  scenario: 'HARNESS';
  input_file: string;
  package_id?: string;
  evaluation_method?: 'llm_as_judge' | 'script_based';
  model_refs: {
    optimizer: string;
    tester: string;
  };
  artifact_path?: never;
}

export interface RsiArtifactTaskCreateParams extends RsiTaskCreateBase {
  scenario: 'ARTIFACT';
  artifact_type: RsiArtifactType;
  input_file?: string;
  model_refs: {
    optimizer: string;
    tester?: string;
  };
  artifact_path?: string;
}

export type RsiTaskCreateParams = RsiHarnessTaskCreateParams | RsiArtifactTaskCreateParams;

export interface RsiTaskCreateResult {
  task_id: string;
  status: RsiTaskStatus;
}

// §6.2 rsi.task.list
export interface RsiTaskListParams {
  scenario?: RsiScenario;
  artifact_type?: RsiArtifactType;
}
export interface RsiTaskListItem {
  task_id: string;
  name: string;
  scenario: RsiScenario;
  artifact_type: RsiArtifactType | null;
  status: RsiTaskStatus;
  // 当前后端 task.list 投影未返回以下字段；P2 推送或本地创建时可补充。
  iter?: { current: number; total: number };
  score?: number | null;
  best?: string | null;
  base?: number | null;
  gain?: number | null;
  running?: boolean;
  created_at: string;
}

// §6.3 rsi.task.get
export interface RsiBestArtifact {
  artifact_id: string;
  name: string;
  adopted: boolean;
}
export interface RsiTaskGetResult {
  task_id: string;
  name: string;
  scenario: RsiScenario;
  artifact_type: RsiArtifactType | null;
  status: RsiTaskStatus;
  config: {
    model: { optimizer: string; tester: string | null };
    input_file: string | null;
    max_iterations: number;
    optimization_instruction: string | null;
    artifact_path: string | null;
    web_proxy_configured: boolean;
  };
  progress: {
    iteration: number;
    total_iterations: number;
    score: number | null;
    baseline: number | null;
  } | null;
  best_artifact: RsiBestArtifact | null;
  usage?: RsiUsage | null;
  failure_reason?: string | null;
}

// §7 训练控制（start/pause/resume/terminate 统一入参 task_id）
export interface RsiTrainingControlResult {
  status: RsiTaskStatus;
}

// §8.1 rsi.report.get
export interface RsiReportGetResult {
  status: RsiTaskStatus;
  best_score: number | null;
  baseline: number | null;
  metrics: {
    eval_passed: number;
    eval_total: number;
    pruned_count?: number | null;
    iterations: number;
    best_artifact_id?: string | null;
  };
  usage: RsiUsage | null;
  best_artifact: RsiBestArtifact | null;
  report_summary: string;
  markdown: string | null; // 前端本期占位，不渲染
}

// §8.2 rsi.usage.get
export interface RsiUsageGetResult {
  usage: RsiUsage;
  per_iteration: Array<{ iteration: number; usage: RsiUsage }>;
  usage_by_node: Record<string, RsiUsage> | null;
}

// §8.3 rsi.artifact.download（HTTP 文件流，前端走 window 触发下载）

// §9.1 rsi.tree.get
export interface RsiNodeChange {
  group?: string; // harness 事件投影
  element?: string; // artifact provider 投影
  operation: string;
  function?: string | null;
  target?: string | null;
  summary?: string | null;
  reason?: string | null;
}
export interface RsiTreeNode {
  node_id: string;
  iteration: number;
  parent_id: string | null; // 根为 null
  type: RsiNodeType;
  adopted: boolean;
  score: number | null;
  description: string | null;
  snapshot_artifact_id: string | null; // 仅被采纳节点
  failure_reason: string | null;
  failure_class: string | null;
  changes: RsiNodeChange[] | null;
  extra: Record<string, unknown> | null; // 产物优化扩展容器
}
export interface RsiTreeGetResult {
  nodes: RsiTreeNode[];
  depth: number;
  iteration: number;
}

// ── 推送事件（P1/P2/P3，§4 清单）──
// 契约文档仅列出清单未给出独立 schema，下列形状按字段惯例（§3.3 status / §3.4 usage /
// §11.1 iteration 派生口径 / §9.2 tree.delta 节点结构）推导，接口 ready 后以服务端为准。

// P1 rsi.training.status.changed —— 状态迁移
export interface RsiTrainingStatusChangedPayload {
  task_id: string;
  status: RsiTaskStatus;
  old_status?: RsiTaskStatus;
  new_status?: RsiTaskStatus;
}

// P2 rsi.training.progress —— 迭代进度 + score + 累计开销
export interface RsiTrainingProgressPayload {
  task_id: string;
  iteration: number;
  total_iterations: number;
  score: number | null;
  baseline: number | null;
  usage: RsiUsage | null;
  progress?: {
    iteration: number;
    total_iterations: number;
    score: number | null;
    baseline: number | null;
  };
}

// P3 rsi.training.tree.delta —— 树增量节点（与 tree.get 同源，§9.2）
export interface RsiTrainingTreeDeltaPayload {
  task_id: string;
  nodes: RsiTreeNode[];
}

export interface RsiArtifactDownloadResult {
  path: string;
  kind: 'harness_plugin' | 'artifact_package';
  is_best: boolean;
  filename: string;
  is_directory?: boolean;
  download_url?: string;
  download_token?: string;
}

export interface RsiArtifactFileEntryResult {
  name: string;
  path: string;
  isDirectory: boolean;
  size: number;
  type: string;
}

export interface RsiArtifactFilesListResult {
  root: string;
  initial_path: string | null;
  files: RsiArtifactFileEntryResult[];
}

export interface RsiArtifactFileGetResult {
  path: string;
  name: string;
  size: number;
  type: string;
  encoding: 'text' | 'base64';
  content: string;
}
