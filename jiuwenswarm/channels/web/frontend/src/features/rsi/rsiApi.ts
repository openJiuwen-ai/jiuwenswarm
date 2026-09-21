/**
 * RSI Web API 客户端。
 *
 * 组件只调用这里的 typed API。这里同时承担两件事：把页面的小写枚举转成
 * AgentServer 的 wire contract，以及把当前服务层/Provider 的字段差异归一为
 * 页面稳定模型。生产链路默认走真实 WebSocket，不再依赖浏览器端假数据。
 */

import { webRequest } from '../../services/webClient';
import { rsiMock } from './mockData';
import type {
  RsiArtifactDownloadResult,
  RsiArtifactFileGetResult,
  RsiArtifactFilesListResult,
  RsiArtifactType,
  RsiDatasetValidateParams,
  RsiDatasetValidateResult,
  RsiNodeChange,
  RsiNodeType,
  RsiReportGetResult,
  RsiScenario,
  RsiTaskCreateParams,
  RsiTaskCreateResult,
  RsiTaskGetResult,
  RsiTaskListItem,
  RsiTaskListParams,
  RsiTaskStatus,
  RsiTrainingControlResult,
  RsiTrainingProgressPayload,
  RsiTrainingStatusChangedPayload,
  RsiTrainingTreeDeltaPayload,
  RsiTreeGetResult,
  RsiTreeNode,
  RsiUsage,
  RsiUsageGetResult,
} from './types';

export const RSI_EVENTS = {
  statusChanged: 'rsi.training.status.changed',
  progress: 'rsi.training.progress',
  treeDelta: 'rsi.training.tree.delta',
} as const;

const METHOD = {
  datasetValidate: 'rsi.dataset.validate',
  taskCreate: 'rsi.task.create',
  taskList: 'rsi.task.list',
  taskGet: 'rsi.task.get',
  taskDelete: 'rsi.task.delete',
  trainingStart: 'rsi.training.start',
  trainingPause: 'rsi.training.pause',
  trainingResume: 'rsi.training.resume',
  trainingTerminate: 'rsi.training.terminate',
  reportGet: 'rsi.report.get',
  usageGet: 'rsi.usage.get',
  artifactDownload: 'rsi.artifact.download',
  artifactFilesList: 'rsi.artifact.files.list',
  artifactFilesGet: 'rsi.artifact.files.get',
  treeGet: 'rsi.tree.get',
  harnessInstall: 'rsi.harness.install',
  harnessVersionsList: 'rsi.harness.versions.list',
  harnessRollback: 'rsi.harness.rollback',
} as const;

const RSI_MOCK_STORAGE_KEY = 'rsi_use_mock';

function isMockEnabled(): boolean {
  try {
    return typeof window !== 'undefined' && window.localStorage.getItem(RSI_MOCK_STORAGE_KEY) === 'true';
  } catch {
    return false;
  }
}

const RSI_SESSION_STORAGE_KEY = 'jiuwenswarm.rsi.session_id';
let inMemoryRsiSessionId: string | null = null;

/** Keep RSI requests and asynchronous Provider pushes on the same browser session. */
function getRsiSessionId(): string {
  if (inMemoryRsiSessionId) return inMemoryRsiSessionId;
  if (typeof window !== 'undefined') {
    try {
      const stored = window.sessionStorage.getItem(RSI_SESSION_STORAGE_KEY)?.trim();
      if (stored) {
        inMemoryRsiSessionId = stored;
        return stored;
      }
    } catch {
      // sessionStorage can be unavailable in privacy mode; use the memory fallback.
    }
  }
  const generated = `rsi_${Date.now().toString(36)}_${Math.random().toString(36).slice(2)}`;
  inMemoryRsiSessionId = generated;
  if (typeof window !== 'undefined') {
    try {
      window.sessionStorage.setItem(RSI_SESSION_STORAGE_KEY, generated);
    } catch {
      // The in-memory value is sufficient for the lifetime of this page.
    }
  }
  return generated;
}

function withRsiSession(params: WireRecord): WireRecord {
  return { ...params, session_id: getRsiSessionId() };
}

type WireRecord = Record<string, unknown>;

function asRecord(value: unknown): WireRecord | null {
  return value && typeof value === 'object' && !Array.isArray(value) ? (value as WireRecord) : null;
}

function asString(value: unknown, fallback = ''): string {
  return typeof value === 'string' ? value : value == null ? fallback : String(value);
}

function asNullableString(value: unknown): string | null {
  const result = asString(value).trim();
  return result ? result : null;
}

function asNumber(value: unknown, fallback = 0): number {
  if (typeof value === 'number' && Number.isFinite(value)) return value;
  if (typeof value === 'string' && value.trim()) {
    const parsed = Number(value);
    if (Number.isFinite(parsed)) return parsed;
  }
  return fallback;
}

function asNullableNumber(value: unknown): number | null {
  if (value == null || value === '') return null;
  const parsed = asNumber(value, Number.NaN);
  return Number.isFinite(parsed) ? parsed : null;
}

function asBoolean(value: unknown, fallback = false): boolean {
  return typeof value === 'boolean' ? value : fallback;
}

export function normalizeRsiScenario(value: unknown, fallback: RsiScenario = 'HARNESS'): RsiScenario {
  const normalized = String(value ?? '')
    .trim()
    .toUpperCase();
  return normalized === 'ARTIFACT' ? 'ARTIFACT' : normalized === 'HARNESS' ? 'HARNESS' : fallback;
}

export function normalizeRsiArtifactType(value: unknown): RsiArtifactType | null {
  const normalized = String(value ?? '')
    .trim()
    .toUpperCase();
  return normalized === 'PAPER' || normalized === 'PROGRAM' ? normalized : null;
}

export function normalizeRsiStatus(value: unknown, fallback: RsiTaskStatus = 'CREATED'): RsiTaskStatus {
  const normalized = String(value ?? '')
    .trim()
    .toUpperCase();
  const statuses: RsiTaskStatus[] = ['CREATED', 'QUEUED', 'RUNNING', 'COMPLETED', 'FAILED', 'PAUSED', 'TERMINATED'];
  return statuses.includes(normalized as RsiTaskStatus) ? (normalized as RsiTaskStatus) : fallback;
}

function normalizeUsage(value: unknown): RsiUsage | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const tokens = asRecord(raw.tokens) ?? {};
  return {
    tokens: {
      input: asNumber(tokens.input),
      output: asNumber(tokens.output),
      cache_hit: asNumber(tokens.cache_hit ?? tokens.cacheHit),
    },
    cost_estimate: asNumber(raw.cost_estimate ?? raw.costEstimate),
    call_count: asNumber(raw.call_count ?? raw.callCount),
  };
}

function normalizeBestArtifact(value: unknown): RsiTaskGetResult['best_artifact'] {
  const raw = asRecord(value);
  if (!raw) return null;
  const artifactId = asNullableString(raw.artifact_id ?? raw.id);
  if (!artifactId) return null;
  return {
    artifact_id: artifactId,
    name: asString(raw.name, artifactId),
    adopted: asBoolean(raw.adopted, true),
  };
}

function normalizeProgress(value: unknown, defaults?: { total?: number }): RsiTaskGetResult['progress'] {
  const raw = asRecord(value) ?? {};
  return {
    iteration: asNumber(raw.iteration ?? raw.current_iteration),
    total_iterations: asNumber(raw.total_iterations ?? raw.total ?? defaults?.total),
    score: asNullableNumber(raw.score),
    baseline: asNullableNumber(raw.baseline ?? raw.base),
  };
}

function normalizeChange(value: unknown): RsiNodeChange | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const group = asNullableString(raw.group);
  const element = asNullableString(raw.element ?? raw.domain);
  const operation = asString(raw.operation).toUpperCase();
  const func = asNullableString(raw.function ?? raw.function_name);
  const target = asNullableString(raw.target);
  const summary = asNullableString(raw.summary);
  const reason = asNullableString(raw.reason ?? raw.description);
  return {
    ...(group ? { group } : {}),
    ...(element ? { element } : {}),
    operation,
    ...(func ? { function: func } : {}),
    ...(target ? { target } : {}),
    ...(summary ? { summary } : {}),
    ...(reason ? { reason } : {}),
  };
}

function normalizeNodeType(value: unknown): RsiNodeType {
  const normalized = String(value ?? '')
    .trim()
    .toUpperCase();
  if (
    normalized === 'ROOT' ||
    normalized === 'ADOPTED' ||
    normalized === 'REJECTED' ||
    normalized === 'PROVISIONAL' ||
    normalized === 'PRUNED'
  ) {
    return normalized;
  }
  if (normalized === 'CANDIDATE' || normalized === 'REPORTING' || normalized === 'SUCCESS') return 'ADOPTED';
  return 'REJECTED';
}

function normalizeTreeNode(value: unknown): RsiTreeNode | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const nodeId = asNullableString(raw.node_id ?? raw.id);
  if (!nodeId) return null;
  const changes = Array.isArray(raw.changes)
    ? raw.changes.map(normalizeChange).filter((item): item is RsiNodeChange => item !== null)
    : null;
  const extra = asRecord(raw.extra);
  const paper = asRecord(extra?.paper);
  const rawType = asString(raw.type).toUpperCase();
  let type =
    rawType === 'REPORTING'
      ? paper?.outcome === 'pending'
        ? 'PROVISIONAL'
        : asBoolean(raw.adopted)
          ? 'ADOPTED'
          : 'REJECTED'
      : normalizeNodeType(raw.type);
  if (rawType === 'CANDIDATE') type = 'PROVISIONAL';
  if (extra?.program != null && rawType === 'ADOPTED' && !asBoolean(raw.adopted)) type = 'REJECTED';
  return {
    node_id: nodeId,
    iteration: asNumber(raw.iteration),
    parent_id: asNullableString(raw.parent_id),
    type,
    adopted: asBoolean(raw.adopted),
    score: asNullableNumber(raw.score),
    description: asNullableString(raw.summary ?? raw.description),
    snapshot_artifact_id: asNullableString(raw.snapshot_artifact_id),
    failure_reason: asNullableString(raw.reason ?? raw.failure_reason),
    failure_class: asNullableString(raw.failure_class),
    changes,
    extra,
  };
}

function normalizeTaskListItem(value: unknown): RsiTaskListItem | null {
  const raw = asRecord(value);
  if (!raw) return null;
  const status = normalizeRsiStatus(raw.status);
  const iter = asRecord(raw.iter);
  const bestArtifact = normalizeBestArtifact(raw.best_artifact);
  return {
    task_id: asString(raw.task_id),
    name: asString(raw.name, asString(raw.task_id, 'RSI 实验')),
    scenario: normalizeRsiScenario(raw.scenario),
    artifact_type: normalizeRsiArtifactType(raw.artifact_type),
    status,
    iter: {
      current: asNumber(iter?.current ?? raw.iteration),
      total: asNumber(iter?.total ?? raw.total_iterations ?? raw.max_iterations),
    },
    score: asNullableNumber(raw.score),
    best: asNullableString(raw.best) ?? bestArtifact?.name ?? bestArtifact?.artifact_id ?? null,
    base: asNullableNumber(raw.base ?? raw.baseline),
    gain: asNullableNumber(raw.gain),
    running: asBoolean(raw.running, status === 'RUNNING'),
    created_at: asString(raw.created_at),
  };
}

function normalizeTask(value: unknown): RsiTaskGetResult {
  const raw = asRecord(value) ?? {};
  const config = asRecord(raw.config) ?? {};
  const model = asRecord(config.model) ?? {};
  const progress = normalizeProgress(raw.progress, { total: asNumber(config.max_iterations) });
  return {
    task_id: asString(raw.task_id),
    name: asString(raw.name, asString(raw.task_id, 'RSI 实验')),
    scenario: normalizeRsiScenario(raw.scenario),
    artifact_type: normalizeRsiArtifactType(raw.artifact_type),
    status: normalizeRsiStatus(raw.status),
    config: {
      model: {
        optimizer: asString(model.optimizer),
        tester: asNullableString(model.tester),
      },
      input_file: asNullableString(config.input_file),
      max_iterations: asNumber(config.max_iterations, 1),
      optimization_instruction: asNullableString(config.optimization_instruction),
      artifact_path: asNullableString(config.artifact_path),
      web_proxy_configured: config.web_proxy_configured === true,
    },
    progress,
    best_artifact: normalizeBestArtifact(raw.best_artifact),
    usage: normalizeUsage(raw.usage),
    failure_reason: asNullableString(raw.failure_reason),
  };
}

function normalizeReport(value: unknown): RsiReportGetResult | null {
  if (value == null) return null;
  const raw = asRecord(value) ?? {};
  const metrics = asRecord(raw.metrics) ?? {};
  const bestArtifact = normalizeBestArtifact(raw.best_artifact);
  return {
    status: normalizeRsiStatus(raw.status),
    best_score: asNullableNumber(raw.best_score),
    baseline: asNullableNumber(raw.baseline),
    metrics: {
      eval_passed: asNumber(metrics.eval_passed),
      eval_total: asNumber(metrics.eval_total),
      pruned_count: asNullableNumber(metrics.pruned_count),
      iterations: asNumber(metrics.iterations),
      best_artifact_id: asNullableString(metrics.best_artifact_id) ?? bestArtifact?.artifact_id ?? null,
    },
    usage: normalizeUsage(raw.usage),
    best_artifact: bestArtifact,
    report_summary: asString(raw.report_summary ?? raw.summary),
    markdown: asNullableString(raw.markdown),
  };
}

function normalizeUsageResult(value: unknown): RsiUsageGetResult | null {
  if (value == null) return null;
  const raw = asRecord(value) ?? {};
  const usage = normalizeUsage(raw.usage);
  if (!usage) return null;
  const perIteration = Array.isArray(raw.per_iteration)
    ? raw.per_iteration.flatMap((item) => {
        const entry = asRecord(item);
        const entryUsage = normalizeUsage(entry?.usage);
        return entryUsage ? [{ iteration: asNumber(entry?.iteration), usage: entryUsage }] : [];
      })
    : [];
  const rawByNode = asRecord(raw.usage_by_node);
  const usageByNode: Record<string, RsiUsage> | null = rawByNode
    ? Object.fromEntries(
        Object.entries(rawByNode).flatMap(([key, item]) => {
          const itemUsage = normalizeUsage(item);
          return itemUsage ? [[key, itemUsage]] : [];
        }),
      )
    : null;
  return { usage, per_iteration: perIteration, usage_by_node: usageByNode };
}

function normalizeTree(value: unknown): RsiTreeGetResult {
  const raw = asRecord(value) ?? {};
  const nodes = Array.isArray(raw.nodes)
    ? raw.nodes.map(normalizeTreeNode).filter((item): item is RsiTreeNode => item !== null)
    : [];
  return {
    nodes,
    depth: asNumber(raw.depth),
    iteration: asNumber(raw.iteration),
  };
}

export function normalizeRsiStatusChangedPayload(value: unknown): RsiTrainingStatusChangedPayload | null {
  const raw = asRecord(value);
  const taskId = asNullableString(raw?.task_id);
  if (!taskId) return null;
  const from = raw?.from ?? raw?.old_status;
  return {
    task_id: taskId,
    status: normalizeRsiStatus(raw?.status ?? raw?.new_status),
    old_status: from != null ? normalizeRsiStatus(from) : undefined,
    new_status: normalizeRsiStatus(raw?.status ?? raw?.new_status),
  };
}

export function normalizeRsiProgressPayload(value: unknown): RsiTrainingProgressPayload | null {
  const raw = asRecord(value);
  const taskId = asNullableString(raw?.task_id);
  if (!taskId) return null;
  const nested = asRecord(raw?.progress) ?? raw ?? {};
  return {
    task_id: taskId,
    iteration: asNumber(raw?.iteration ?? nested.iteration),
    total_iterations: asNumber(raw?.total ?? raw?.total_iterations ?? nested.total ?? nested.total_iterations),
    score: asNullableNumber(raw?.score ?? nested.score),
    baseline: asNullableNumber(raw?.baseline ?? nested.baseline),
    usage: normalizeUsage(raw?.usage ?? nested.usage),
  };
}

export function normalizeRsiTreeDeltaPayload(value: unknown): RsiTrainingTreeDeltaPayload | null {
  const raw = asRecord(value);
  const taskId = asNullableString(raw?.task_id);
  if (!taskId) return null;
  const rawNodes = Array.isArray(raw?.nodes) ? raw.nodes : [];
  return {
    task_id: taskId,
    nodes: rawNodes.map(normalizeTreeNode).filter((item): item is RsiTreeNode => item !== null),
  };
}

function toWireScenario(value: RsiScenario | undefined): string {
  return String(value ?? 'HARNESS').toUpperCase();
}

function toWireArtifactType(value: RsiArtifactType | undefined): string | undefined {
  return value ? value.toUpperCase() : undefined;
}

export interface RsiModelOption {
  id: string;
  name: string;
  is_free: boolean;
  provider?: string;
}

export interface RsiHarnessVersion {
  installation_id: string;
  task_id: string | null;
  node_id: string | null;
  harness_name: string;
  sha256: string;
  installed_at: string | null;
  is_active: boolean;
  is_initial: boolean;
  available: boolean;
}

export interface RsiHarnessVersionsResult {
  active_installation_id: string | null;
  versions: RsiHarnessVersion[];
}

export interface RsiHarnessActivationResult {
  installation_id: string;
  task_id: string;
  node_id: string | null;
  harness_name: string;
  sha256: string;
  status: 'ACTIVE';
  already_active: boolean;
  from_installation_id?: string | null;
  rolled_back?: boolean;
}

function normalizeHarnessActivation(value: unknown): RsiHarnessActivationResult {
  const raw = asRecord(value) ?? {};
  return {
    installation_id: asString(raw.installation_id),
    task_id: asString(raw.task_id),
    node_id: asNullableString(raw.node_id),
    harness_name: asString(raw.harness_name),
    sha256: asString(raw.sha256),
    status: 'ACTIVE',
    already_active: raw.already_active === true,
    from_installation_id: asNullableString(raw.from_installation_id),
    rolled_back: raw.rolled_back === true,
  };
}

export function rsiHarnessInstall(taskId: string): Promise<RsiHarnessActivationResult> {
  return webRequest<unknown>(METHOD.harnessInstall, withRsiSession({ task_id: taskId })).then(
    normalizeHarnessActivation,
  );
}

export function rsiHarnessVersionsList(): Promise<RsiHarnessVersionsResult> {
  return webRequest<unknown>(METHOD.harnessVersionsList, withRsiSession({})).then((value) => {
    const raw = asRecord(value) ?? {};
    const versions = Array.isArray(raw.versions) ? raw.versions : [];
    return {
      active_installation_id: asNullableString(raw.active_installation_id),
      versions: versions.map((value) => {
        const version = asRecord(value) ?? {};
        return {
          installation_id: asString(version.installation_id),
          task_id: asNullableString(version.task_id),
          node_id: asNullableString(version.node_id),
          harness_name: asString(version.harness_name),
          sha256: asString(version.sha256),
          installed_at: asNullableString(version.installed_at),
          is_active: version.is_active === true,
          is_initial: version.is_initial === true,
          available: version.available === true,
        };
      }),
    };
  });
}

export function rsiHarnessRollback(installationId: string): Promise<RsiHarnessActivationResult> {
  return webRequest<unknown>(
    METHOD.harnessRollback,
    withRsiSession({
      installation_id: installationId,
    }),
  ).then(normalizeHarnessActivation);
}

export async function rsiListModels(): Promise<RsiModelOption[]> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.modelList);
  }
  try {
    const response = await webRequest<unknown>('models.list');
    const raw = asRecord(response);
    const models = Array.isArray(raw?.models) ? raw.models : [];
    return models.flatMap((item, index) => {
      const model = asRecord(item);
      if (!model) return [];
      const id = asString(model.model_name, `model-${index}`);
      return [
        {
          id,
          name: asString(model.alias, id),
          is_free: model.is_free === true,
          provider: asNullableString(model.model_provider) ?? undefined,
        },
      ];
    });
  } catch {
    return [];
  }
}

export function rsiDatasetValidate(params: RsiDatasetValidateParams): Promise<RsiDatasetValidateResult> {
  if (isMockEnabled()) {
    return rsiMock.delay({ valid: true, sample_count: 2288, errors: [] });
  }
  const wire: Record<string, unknown> = {
    input_file: params.input_file,
    scenario: toWireScenario(params.scenario),
  };
  const artifactType = toWireArtifactType(params.scenario === 'ARTIFACT' ? params.artifact_type : undefined);
  if (artifactType) wire.artifact_type = artifactType;
  return webRequest<unknown>(METHOD.datasetValidate, withRsiSession(wire)).then((value) => {
    const raw = asRecord(value) ?? {};
    const errors = Array.isArray(raw.errors)
      ? raw.errors.flatMap((item) => {
          const error = asRecord(item);
          return error
            ? [
                {
                  reason: asString(error.reason ?? error.message, '输入校验失败'),
                  code: asString(error.code, 'DATASET_INVALID'),
                },
              ]
            : [];
        })
      : [];
    return {
      valid: raw.valid === true,
      sample_count: raw.sample_count == null ? null : asNumber(raw.sample_count),
      errors,
    };
  });
}

export function rsiTaskCreate(params: RsiTaskCreateParams): Promise<RsiTaskCreateResult> {
  if (isMockEnabled()) {
    return rsiMock.delay({
      task_id: 'rsi-task-001',
      status: 'CREATED',
    });
  }
  const wire: Record<string, unknown> = {
    scenario: toWireScenario(params.scenario),
    name: params.name,
    model_refs: params.model_refs,
  };
  const artifactType = toWireArtifactType(params.scenario === 'ARTIFACT' ? params.artifact_type : undefined);
  if (artifactType) wire.artifact_type = artifactType;
  if (params.scenario === 'HARNESS' && params.package_id?.trim()) wire.package_id = params.package_id;
  if (params.scenario === 'HARNESS' && params.evaluation_method) wire.evaluation_method = params.evaluation_method;
  if (params.input_file?.trim()) {
    // Harness 的公共契约字段是 input_file；Paper 也允许把数据集作为 input_file。
    wire.input_file = params.input_file;
  }
  if (params.artifact_path?.trim()) wire.artifact_path = params.artifact_path;
  if (params.max_iterations != null) wire.max_iterations = params.max_iterations;
  if (params.optimization_instruction?.trim()) wire.optimization_instruction = params.optimization_instruction;
  if (params.web_proxy?.trim()) wire.web_proxy = params.web_proxy.trim();
  return webRequest<unknown>(METHOD.taskCreate, withRsiSession(wire)).then((value) => {
    const raw = asRecord(value) ?? {};
    return {
      task_id: asString(raw.task_id),
      status: normalizeRsiStatus(raw.status),
    };
  });
}

const normalizeTaskListPayload = (value: unknown): RsiTaskListItem[] => {
  const tasks = asRecord(value)?.tasks;
  return (Array.isArray(tasks) ? tasks : []).flatMap((item) => {
    const normalized = normalizeTaskListItem(item);
    return normalized ? [normalized] : [];
  });
};

export function rsiTaskList(params: RsiTaskListParams = {}): Promise<RsiTaskListItem[]> {
  if (isMockEnabled()) {
    let list = [...rsiMock.tasks];
    if (params.scenario) list = list.filter((item) => item.scenario === params.scenario);
    if (params.artifact_type) list = list.filter((item) => item.artifact_type === params.artifact_type);
    return rsiMock.delay({ tasks: list }).then(normalizeTaskListPayload);
  }
  const wire: Record<string, unknown> = {};
  if (params.scenario) wire.scenario = toWireScenario(params.scenario);
  if (params.artifact_type) wire.artifact_type = toWireArtifactType(params.artifact_type);
  return webRequest<unknown>(METHOD.taskList, withRsiSession(wire)).then(normalizeTaskListPayload);
}

export function rsiTaskGet(taskId: string): Promise<RsiTaskGetResult> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.taskGet(taskId) ?? rsiMock.taskGet('rsi-task-001'));
  }
  return webRequest<unknown>(METHOD.taskGet, withRsiSession({ task_id: taskId })).then(normalizeTask);
}

export function rsiTaskDelete(taskId: string): Promise<{ ok: boolean }> {
  if (isMockEnabled()) {
    return rsiMock.delay({ ok: true });
  }
  return webRequest<unknown>(METHOD.taskDelete, withRsiSession({ task_id: taskId })).then((value) => ({
    ok: asRecord(value)?.ok === true,
  }));
}

function trainingControl(method: string, taskId: string): Promise<RsiTrainingControlResult> {
  if (isMockEnabled()) {
    const statuses: Record<string, RsiTaskStatus> = {
      [METHOD.trainingStart]: 'RUNNING',
      [METHOD.trainingPause]: 'PAUSED',
      [METHOD.trainingResume]: 'QUEUED',
      [METHOD.trainingTerminate]: 'TERMINATED',
    };
    return rsiMock.delay({ status: statuses[method] ?? 'RUNNING' });
  }
  return webRequest<unknown>(method, withRsiSession({ task_id: taskId })).then((value) => ({
    status: normalizeRsiStatus(asRecord(value)?.status),
  }));
}

export function rsiTrainingStart(taskId: string): Promise<RsiTrainingControlResult> {
  return trainingControl(METHOD.trainingStart, taskId);
}

export function rsiTrainingPause(taskId: string): Promise<RsiTrainingControlResult> {
  return trainingControl(METHOD.trainingPause, taskId);
}

export function rsiTrainingResume(taskId: string): Promise<RsiTrainingControlResult> {
  return trainingControl(METHOD.trainingResume, taskId);
}

export function rsiTrainingTerminate(taskId: string): Promise<RsiTrainingControlResult> {
  return trainingControl(METHOD.trainingTerminate, taskId);
}

export function rsiReportGet(taskId: string): Promise<RsiReportGetResult | null> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.report(taskId) ?? null);
  }
  return webRequest<unknown>(METHOD.reportGet, withRsiSession({ task_id: taskId })).then(normalizeReport);
}

export function rsiUsageGet(taskId: string): Promise<RsiUsageGetResult | null> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.usage(taskId) ?? null);
  }
  return webRequest<unknown>(METHOD.usageGet, withRsiSession({ task_id: taskId }))
    .then(normalizeUsageResult)
    .catch((error: unknown) => {
      // CREATED 阶段还没有 usage snapshot；服务层目前用 TASK_NOT_FOUND 表示该状态。
      const code = asRecord(error)?.code;
      if (code === 'TASK_NOT_FOUND' || code === 'INTERNAL_ERROR') return null;
      throw error;
    });
}

export function rsiArtifactDownload(taskId: string, artifactId?: string): Promise<RsiArtifactDownloadResult> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.artifactDownload(taskId, artifactId));
  }
  const params: Record<string, unknown> = { task_id: taskId };
  if (artifactId) params.artifact_id = artifactId;
  return webRequest<unknown>(METHOD.artifactDownload, withRsiSession(params)).then((value) => {
    const raw = asRecord(value) ?? {};
    return {
      path: asString(raw.path),
      kind: asString(raw.kind) === 'artifact_package' ? 'artifact_package' : 'harness_plugin',
      is_best: raw.is_best === true,
      filename: asString(raw.filename, asString(raw.path).split(/[\\/]/).pop() || 'download'),
      is_directory: raw.is_directory === true,
      download_url: asNullableString(raw.download_url) ?? undefined,
      download_token: asNullableString(raw.download_token) ?? undefined,
    };
  });
}

export function rsiArtifactDownloadUrl(result: RsiArtifactDownloadResult): string | null {
  return result.download_url ?? null;
}

export function rsiArtifactFilesList(taskId: string, path: string): Promise<RsiArtifactFilesListResult> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.artifactFilesList(taskId, path));
  }
  return webRequest<RsiArtifactFilesListResult>(
    METHOD.artifactFilesList,
    withRsiSession({
      task_id: taskId,
      path,
    }),
  );
}

export function rsiArtifactFilesGet(taskId: string, path: string): Promise<RsiArtifactFileGetResult> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.artifactFilesGet(taskId, path));
  }
  return webRequest<RsiArtifactFileGetResult>(
    METHOD.artifactFilesGet,
    withRsiSession({
      task_id: taskId,
      path,
    }),
  );
}

export function rsiTreeGet(taskId: string): Promise<RsiTreeGetResult> {
  if (isMockEnabled()) {
    return rsiMock.delay(rsiMock.tree(taskId));
  }
  return webRequest<unknown>(METHOD.treeGet, withRsiSession({ task_id: taskId })).then(normalizeTree);
}
