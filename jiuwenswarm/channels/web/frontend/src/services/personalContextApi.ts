/**
 * PersonalContext (主动上下文) 前端 API 薄封装。
 *
 * 全部走 webClient WS 通道（与 command.goal / skills.graph.get 同一条单例 webClient），
 * 方法名与后端 ReqMethod 1:1 对应（见 jiuwenswarm/common/schema/message.py）。
 * 非流式方法直接用 webRequest；图谱拉取走流式 stream_graph（sendFireAndForget + webClient.on
 * 订阅 start/nodes/edges/end 四事件），见 getGraph。
 */

import { webClient, webRequest } from './webClient';

// ── 与后端 PersonalContextStatus.model_dump() 对齐 ──────────────────────
export type PersonalContextRuntimeState =
  | 'CREATED'
  | 'CONFIGURED'
  | 'STARTING'
  | 'RUNNING'
  | 'STOPPING'
  | 'STOPPED'
  | 'FAILED';

export type FetchServiceState =
  | 'STOPPED'
  | 'STARTING'
  | 'RUNNING'
  | 'STOPPING'
  | 'FAILED';

/** 后端 fetch_run_progress[id].run_state 用小写值（与 fetch_service_states 的大写枚举是两套体系）。 */
export type FetchRunState =
  | 'idle'
  | 'running'
  | 'stopping'
  | 'succeeded'
  | 'cancelled'
  | 'failed';

/** 单服务采集进度（后端 get_fetch_run_status / status.fetch_run_progress[id]）。 */
export type FetchRunProgress = {
  service_id: string;
  run_state: FetchRunState;
  progress_percent: number;
  total_items: number;
  completed_items: number;
  last_error: string | null;
};

/** get_fetch_run_status 返回的单次运行记录；终端记录会保留 run_id 和起止时间。 */
export type FetchRunRecord = FetchRunProgress & {
  run_id: string;
  started_at: string;
  finished_at: string | null;
};

export type FetchRunStatusService = {
  service_id: string;
  runs: FetchRunRecord[];
};

export type FetchRunStatusResponse = {
  services: FetchRunStatusService[];
};

export type PersonalContextStatus = {
  configured: boolean;
  collection_enabled: boolean;
  agent_use_enabled: boolean;
  state: PersonalContextRuntimeState;
  pipeline_running: boolean;
  pipeline_queue_size: number;
  fetch_service_states: Record<string, FetchServiceState>;
  fetch_service_errors: Record<string, string | null>;
  fetch_run_progress: Record<string, FetchRunProgress>;
  context_root: string;
  context_ready: boolean;
  last_error: {
    code: number;
    status: string;
    message: string;
    operation: string;
  } | null;
};

/**
 * 运行时停止超时不属于图谱发布失败；它应由采集任务页处理。
 * Core 目前会把 timeout 模板参数缺失渲染成 `<missing:timeout>`，所以这里只匹配稳定字段。
 */
export function isFetchStopTimeoutError(
  error: PersonalContextStatus['last_error'] | undefined,
): boolean {
  return (
    error?.status === 'CONTEXT_PROACTIVE_RUNTIME_TIMEOUT' &&
    error.operation === 'deactivate_runtime' &&
    error.message.includes('PersonalContext stop timed out')
  );
}

// ── runtime.get_config / patch / select_model 返回的 stored config ─────────
export type StrategyProfile = 'rules' | 'balanced' | 'agent';

export type FetchProvider =
  | 'local_files'
  | 'github'
  | 'gitcode'
  | 'feishu'
  | 'browser_bookmarks'
  | 'zhihu_reader'
  | 'toutiao_reader';

// 后端 _normalize_time_range 接受三种 mode：all（全量）/ recent（近 N 天）/ fixed（时间区间）。
export type TimeRangeMode = 'all' | 'recent' | 'fixed';

export type TimeRange =
  | { mode: 'all' }
  | { mode: 'recent'; recent_days: number }
  | { mode: 'fixed'; start_at: string; end_at: string };

export type FetchServiceConfig = {
  service_id: string;
  provider: FetchProvider;
  enabled: boolean;
  interval_seconds: number;
  max_items_per_run: number | null;
  time_range: TimeRange;
  source: Record<string, unknown>;
  /**
   * 凭证由后端 provider 授权托管（github/gitcode 的 token/pat、飞书 OAuth），
   * 创建时前端不传；list 返回后端也会 _project_service 剥离该字段。故设为可选。
   */
  credentials?: Record<string, string>;
};

/**
 * patch_service 可写字段（对齐后端 host_api.patch_fetch_service 的 allowed 集合）。
 * 名称/来源不可改；credentials 由后端 provider 授权托管，也不可写。
 */
export type FetchServicePatch = Partial<
  Pick<
    FetchServiceConfig,
    'interval_seconds' | 'max_items_per_run' | 'source' | 'time_range'
  >
>;

export type PersonalContextConfig = {
  configured: boolean;
  collection_enabled: boolean;
  agent_use_enabled: boolean;
  strategy_profile: StrategyProfile;
  model_index: number | null;
  model_id: string | null;
  fetch_services: FetchServiceConfig[];
};

// ── 授权结果 ──────────────────────────────────────────────────────────────
export type AuthorizationState =
  | 'not_authorized'
  | 'authorization_required'
  | 'authorizing'
  | 'authorized'
  | 'authorization_failed';

export type AuthorizationResult = {
  provider: string;
  state: AuthorizationState;
  verification_url: string | null;
  expires_at: string | null;
  error: string | null;
};

// ── Context 图 ────────────────────────────────────────────────────────────
export type ContextNodeKind = 'directory' | 'document' | 'source';

export type ContextNode = {
  id: string;
  kind: ContextNodeKind;
  subkind: string;
  label: string;
  path: string;
  service_id: string | null;
  /** directory 节点是否有子节点（后端 stream_graph 携带）。 */
  has_children?: boolean;
};

export type ContextEdge = {
  source: string;
  target: string;
  kind: string;
};

export type ContextGraph = {
  context_ready: boolean;
  nodes: ContextNode[];
  edges: ContextEdge[];
};

export type ContextSearchResultItem = {
  node_id: string;
  title: string;
  path: string;
  snippet: string;
};

export type ContextGraphNodeDetail = {
  node_id: string;
  title: string;
  path: string;
  markdown: string;
};

/**
 * get_source 返回：原子来源的元信息（非 markdown 正文）。
 * 后端 read_source_detail 返回，用于点击 source 链接时展示来源卡片。
 */
export type ContextSourceDetail = {
  source_id: string;
  title: string;
  source_type: string;
  locator: string;
  provider: string;
  service_id: string | null;
  first_seen: string;
  last_seen: string;
};

// ── API 方法 ──────────────────────────────────────────────────────────────
/**
 * 采集单次运行/停止 RPC 的客户端超时。
 * run_fetch 虽为立即返回 accepted，但受后端 _operation_lock 串行影响；stop_fetch_run 会
 * await 采集任务真正落停。两者都放宽到 60s，避免默认 15s 造成的"后端其实已受理，前端却误报请求超时"。
 */
const FETCH_OP_TIMEOUT_MS = 60_000;

/**
 * 配置变更类 RPC 的客户端超时。create_service 受 _operation_lock 串行，且运行时会
 * 先 deactivate（上限 30s）再重建，可能与正在等待/执行的 stop 叠加，故放宽到 90s。
 */
const FETCH_CONFIG_TIMEOUT_MS = 90_000;

/** 运行历史只读且数据量很小；超时后轮询会跳过后续周期，避免堆积请求。 */
const FETCH_RUN_STATUS_TIMEOUT_MS = 15_000;

export const pcApi = {
  getStatus: () =>
    webRequest<PersonalContextStatus>('personal_context.runtime.status'),

  startRuntime: () =>
    webRequest<PersonalContextConfig>(
      'personal_context.runtime.start_collection',
      {},
      // start_collection 会加载 embedding / activate_runtime，且受 _operation_lock 串行，
      // 可能慢于默认 15s；stop_collection 后端会 await 到 _STOP_TIMEOUT_SECONDS(30s) 才返回，
      // 故起停都放宽到 60s，避免"后端其实已停完/起完，前端却先报请求超时"。
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  stopRuntime: () =>
    webRequest<PersonalContextConfig>(
      'personal_context.runtime.stop_collection',
      {},
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  startAgentUse: () =>
    webRequest<PersonalContextConfig>(
      'personal_context.runtime.start_agent_use',
      {},
      // 走 _operation_lock，与慢速 stop_collection 串行时可能被拖慢，统一放宽。
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  stopAgentUse: () =>
    webRequest<PersonalContextConfig>(
      'personal_context.runtime.stop_agent_use',
      {},
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  getConfig: () =>
    webRequest<PersonalContextConfig>('personal_context.runtime.get_config'),

  patchConfig: (patch: { strategy_profile?: StrategyProfile }) =>
    webRequest<PersonalContextConfig>(
      'personal_context.runtime.patch_config',
      { patch },
      { timeoutMs: FETCH_CONFIG_TIMEOUT_MS },
    ),

  selectModel: (model_index: number) =>
    webRequest<PersonalContextConfig>(
      'personal_context.runtime.select_model',
      { model_index },
      { timeoutMs: FETCH_CONFIG_TIMEOUT_MS },
    ),

  listServices: () =>
    webRequest<{ services: FetchServiceConfig[] }>(
      'personal_context.fetch.list_services',
    ),

  createService: (service: FetchServiceConfig) =>
    webRequest<FetchServiceConfig>('personal_context.fetch.create_service', {
      service,
    }, { timeoutMs: FETCH_CONFIG_TIMEOUT_MS }),

  deleteService: (service_id: string) =>
    webRequest<{ ok: true }>('personal_context.fetch.delete_service', {
      service_id,
    }, { timeoutMs: FETCH_CONFIG_TIMEOUT_MS }),

  patchService: (service_id: string, patch: FetchServicePatch) =>
    webRequest<FetchServiceConfig>('personal_context.fetch.patch_service', {
      service_id,
      patch,
    }, { timeoutMs: FETCH_CONFIG_TIMEOUT_MS }),

  startService: (service_id: string) =>
    webRequest<{ ok: true }>('personal_context.fetch.start_service', {
      service_id,
    }, { timeoutMs: FETCH_CONFIG_TIMEOUT_MS }),

  stopService: (service_id: string) =>
    webRequest<{ ok: true }>('personal_context.fetch.stop_service', {
      service_id,
    }, { timeoutMs: FETCH_CONFIG_TIMEOUT_MS }),

  runOne: (service_id: string) =>
    webRequest<{ state: string; service_ids: string[] }>(
      'personal_context.fetch.run_one',
      { service_id },
      // run_fetch 后端立即返回 accepted，但受 _operation_lock 串行影响，极端下可能慢于默认 15s。
      // 放宽超时避免"后端其实已接受/完成，前端却先报请求超时"的假象（同 connectorApi 的教训）。
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  stopRun: (service_id: string) =>
    webRequest<{ ok: true }>(
      'personal_context.fetch.stop_run',
      { service_id },
      // stop_fetch_run 会 await 采集任务真正落停（asyncio.shield），耗时随采集进度不定，放宽超时。
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  getRunStatus: () =>
    webRequest<FetchRunStatusResponse>(
      'personal_context.fetch.get_run_status',
      {},
      { timeoutMs: FETCH_RUN_STATUS_TIMEOUT_MS },
    ),

  getAuthStatus: (provider: string) =>
    webRequest<AuthorizationResult>(
      'personal_context.fetch.get_authorization_status',
      { provider },
      // 后端会真连 GitHub/GitCode 探活（_validate_repository_pat，15s 超时），
      // 走 _operation_lock 串行时可能慢于前端默认 15s，放宽避免"后端已成功，前端却先报超时"。
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  authorizeProvider: (provider: string, credentials?: Record<string, string>) =>
    webRequest<AuthorizationResult>(
      'personal_context.fetch.authorize_provider',
      {
        provider,
        ...(credentials ? { credentials } : {}),
      },
      { timeoutMs: FETCH_OP_TIMEOUT_MS },
    ),

  /**
   * 拉取上下文图谱（流式 stream_graph）。
   *
   * 后端 stream_graph 是 is_stream=true 的流式接口，产出 4 类事件：
   * - personal_context.context.start: {context_ready, root_id, depth}
   * - personal_context.context.nodes: {nodes: ContextNode[]}（每批 ≤200，可能多帧）
   * - personal_context.context.edges: {edges: ContextEdge[]}（每批 ≤200，可能多帧）
   * - personal_context.context.end: {node_count, edge_count}（终止帧）
   * 网关把每个 chunk 转成 {type:'event', event:<event_type>, payload:<delta>}，
   * 前端订阅事件名即可收到（见 webClient.normalizeIncoming / web_connect._serialize_frame）。
   *
   * 用 sendFireAndForget 发出（流式无传统 res），webClient.on 订阅 4 事件，
   * end 帧时 resolve 拼装出的 ContextGraph；错误或超时时 reject 并清理订阅。
   */
  getGraph: () =>
    new Promise<ContextGraph>((resolve, reject) => {
      const nodes: ContextNode[] = [];
      const edges: ContextEdge[] = [];
      let contextReady = false;
      let settled = false;

      const cleanups: Array<() => void> = [];
      // 超时兜底：流式卡住不发 end 帧时，避免 Promise 永悬
      const timeoutId = window.setTimeout(() => {
        if (settled) return;
        settled = true;
        cleanups.splice(0).forEach((fn) => fn());
        reject(new Error('personal_context.context.stream_graph timed out'));
      }, 60_000);

      const finish = (ok: boolean, result?: ContextGraph, err?: unknown) => {
        if (settled) return;
        settled = true;
        window.clearTimeout(timeoutId);
        cleanups.splice(0).forEach((fn) => fn());
        if (ok && result) resolve(result);
        else reject(err ?? new Error('personal_context.context.stream_graph failed'));
      };

      cleanups.push(
        webClient.on<{ context_ready?: boolean }>(
          'personal_context.context.start',
          ({ payload }) => {
            contextReady = Boolean(payload.context_ready);
          },
        ),
        webClient.on<{ nodes?: ContextNode[] }>(
          'personal_context.context.nodes',
          ({ payload }) => {
            if (Array.isArray(payload.nodes)) nodes.push(...payload.nodes);
          },
        ),
        webClient.on<{ edges?: ContextEdge[] }>(
          'personal_context.context.edges',
          ({ payload }) => {
            if (Array.isArray(payload.edges)) edges.push(...payload.edges);
          },
        ),
        webClient.on<{ node_count?: number; edge_count?: number }>(
          'personal_context.context.end',
          () => {
            finish(true, { context_ready: contextReady, nodes, edges });
          },
        ),
        webClient.on<{ error?: string; message?: string }>(
          'chat.error',
          ({ payload }) => {
            finish(false, undefined, new Error(payload.error ?? payload.message ?? 'stream error'));
          },
        ),
      );

      // depth 显式传满（后端上限 10），否则默认 3 会截断深层子目录，
      // 导致左侧文件树与画布节点都丢失更深的文件信息。
      webClient
        .sendFireAndForget('personal_context.context.stream_graph', { depth: 10 }, { isStream: true })
        .catch((e: unknown) => {
          finish(false, undefined, e instanceof Error ? e : new Error(String(e)));
        });
    }),

  searchPages: (query: string) =>
    webRequest<{ results: ContextSearchResultItem[] }>(
      'personal_context.context.search_pages',
      { query },
    ),

  getNode: (node_id: string) =>
    webRequest<ContextGraphNodeDetail>('personal_context.context.get_node', {
      node_id,
    }),

  /**
   * 查询原子来源详情（点击 markdown 中 `[来源N](../source-meta/src_xxx.md)` 用）。
   * 返回来源元信息（title/locator/provider…），非 markdown 正文。
   */
  getSource: (source_id: string) =>
    webRequest<ContextSourceDetail>('personal_context.context.get_source', {
      source_id,
    }),
};

/** provider → 本地化标签 key（i18n）。 */
export const PROVIDER_LABEL_KEYS: Record<FetchProvider, string> = {
  local_files: 'personalContext.provider.localFiles',
  github: 'personalContext.provider.github',
  gitcode: 'personalContext.provider.gitcode',
  feishu: 'personalContext.provider.feishu',
  browser_bookmarks: 'personalContext.provider.browserBookmarks',
  zhihu_reader: 'personalContext.provider.zhihuReader',
  toutiao_reader: 'personalContext.provider.toutiaoReader',
};

/**
 * provider 展示顺序（单一事实源）。
 * 内容页左侧分类列表与「添加内容」下拉共用，避免两处顺序不一致。
 * 顺序：本地文件夹 → Edge 收藏夹 → 知乎专栏 → 今日头条 → 飞书 → GitHub → GitCode。
 */
export const PROVIDER_ORDER: readonly FetchProvider[] = [
  'local_files',
  'browser_bookmarks',
  'zhihu_reader',
  'toutiao_reader',
  'feishu',
  'github',
  'gitcode',
];

/** 采集模式下拉选项（智能体默认置顶，规则模式放在最末）。 */
export const STRATEGY_OPTIONS: StrategyProfile[] = [
  'agent',
  'balanced',
  'rules',
];

/** GitHub 可采集资源，与后端 _GITHUB_RESOURCES 对齐（config.py:30）。 */
export const GITHUB_RESOURCES = ['readme', 'issues', 'pull_requests', 'commits', 'code'] as const;
export type GithubResource = (typeof GITHUB_RESOURCES)[number];

/** GitHub 资源 → 本地化 label key。 */
export const GITHUB_RESOURCE_LABEL_KEYS: Record<GithubResource, string> = {
  readme: 'personalContext.addContent.github.readme',
  issues: 'personalContext.addContent.github.issues',
  pull_requests: 'personalContext.addContent.github.pullRequests',
  commits: 'personalContext.addContent.github.commits',
  code: 'personalContext.addContent.github.code',
};

/** GitCode 可采集资源，与后端 _REPOSITORY_RESOURCES 对齐（config.py:33，与 GitHub 同一套）。 */
export const GITCODE_RESOURCES = ['readme', 'issues', 'pull_requests', 'commits', 'code'] as const;
export type GitcodeResource = (typeof GITCODE_RESOURCES)[number];

/** GitCode 资源 → 本地化 label key。 */
export const GITCODE_RESOURCE_LABEL_KEYS: Record<GitcodeResource, string> = {
  readme: 'personalContext.addContent.gitcode.readme',
  issues: 'personalContext.addContent.gitcode.issues',
  pull_requests: 'personalContext.addContent.gitcode.pullRequests',
  commits: 'personalContext.addContent.gitcode.commits',
  code: 'personalContext.addContent.gitcode.code',
};

/**
 * 飞书采集模式，与后端 config.py:_normalize_service_source feishu 分支对齐。
 * - account：按 resources(docs/tasks/calendar) 采集账号内容
 * - wiki_space：采集指定知识空间（需 wiki_space_id）
 */
export const FEISHU_MODES = ['account', 'wiki_space'] as const;
export type FeishuMode = (typeof FEISHU_MODES)[number];

/** 飞书 account 模式可采集资源，与后端 _FEISHU_RESOURCES 对齐（config.py:31）。 */
export const FEISHU_RESOURCES = ['docs', 'tasks', 'calendar'] as const;
export type FeishuResource = (typeof FEISHU_RESOURCES)[number];

/** 飞书资源 → 本地化 label key。 */
export const FEISHU_RESOURCE_LABEL_KEYS: Record<FeishuResource, string> = {
  docs: 'personalContext.addContent.feishu.docs',
  tasks: 'personalContext.addContent.feishu.tasks',
  calendar: 'personalContext.addContent.feishu.calendar',
};

/** 采集频率单位选项。 */
export const FREQUENCY_OPTIONS = ['hour', 'day'] as const;
export type FrequencyUnit = (typeof FREQUENCY_OPTIONS)[number];

/** 频率单位 → 秒。 */
export const FREQUENCY_SECONDS: Record<FrequencyUnit, number> = {
  hour: 3600,
  day: 86400,
};

/**
 * 单次最大采集条数，对齐后端 config.py: max_items_per_run int|None，ge=1, le=10000。
 * None（前端留空）= 用各 provider 默认值；填值须在 [1,10000]。
 * （后端不接受 0；前端以留空表达"不限/用默认"。）
 */
export const MAX_ITEMS_MIN = 1;
export const MAX_ITEMS_MAX = 10000;

/**
 * 采集频率上限（秒），对齐后端 PersonalContextFetchServiceConfig.interval_seconds 的 le=31_536_000（365 天）。
 * 前端 spinner 递增时据此钳制，避免 freqValue × 86400 撞后端上限报「invalid PersonalContext configuration」。
 */
export const INTERVAL_MAX_SECONDS = 31_536_000;

/**
 * service_id 前端预校验，对齐后端 _safe_segment（config.py: ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$，禁 ./..）。
 * 返回 null 表示通过；否则返回错误信息。
 */
export function validateServiceId(value: string): string | null {
  const text = value.trim();
  if (!text) return 'service_id is required';
  if (text === '.' || text === '..') return 'service_id must not be . or ..';
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(text)) {
    return 'service_id must start with a letter or digit and contain only letters, digits, . _ - (max 128 chars)';
  }
  return null;
}

/**
 * 今日头条 profile_url 前端预校验，对齐后端 _normalize_url + path 校验
 * （config.py 的 toutiao_reader 分支）：
 * - 必须 https，host 为 toutiao.com 或子域
 * - 无 userinfo/query/fragment/端口
 * - path 以 /c/user/token/ 开头，恰好四段 c/user/token/<id>
 * - 允许尾部 / （后端会保留）
 *
 * 返回 null 表示通过；否则返回错误信息。
 */
export function validateToutiaoProfileUrl(value: string): string | null {
  const text = value.trim();
  if (!text) return 'profile_url is required';
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return 'profile_url must be a valid URL';
  }
  if (url.protocol !== 'https:') return 'profile_url must be an https URL';
  if (url.username || url.password) return 'profile_url must not contain userinfo';
  if (url.port) return 'profile_url must not contain a custom port';
  if (url.search || url.hash) return 'profile_url must not contain query or fragment';
  const host = url.hostname.toLowerCase();
  if (host !== 'toutiao.com' && !host.endsWith('.toutiao.com')) {
    return 'profile_url must point to toutiao.com';
  }
  const parts = url.pathname.split('/').filter(Boolean);
  if (parts.length !== 4 || parts[0] !== 'c' || parts[1] !== 'user' || parts[2] !== 'token' || !parts[3]) {
    return 'profile_url must be a Toutiao profile homepage URL (https://www.toutiao.com/c/user/token/<id>)';
  }
  return null;
}

/**
 * GitHub 仓库 URL → {owner, repo} 解析。
 * 接受 https://github.com/<owner>/<repo> 或 git@github.com:<owner>/<repo>(.git)
 * 对齐后端 _safe_segment 规则：owner/repo 须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
 * 返回 null + 不合法时返回错误信息。
 */
export function parseGithubRepoUrl(value: string): { owner: string; repo: string } | { error: string } {
  const text = value.trim();
  if (!text) return { error: 'github repo url is required' };
  let owner = '';
  let repo = '';
  const m = text.match(/^https?:\/\/(?:[^/]*\.)?github\.com\/([^/]+)\/([^/?#]+)/i);
  if (m) {
    owner = m[1];
    repo = m[2];
  } else {
    const m2 = text.match(/^git@github\.com:([^/]+)\/([^?#]+)$/i);
    if (m2) {
      owner = m2[1];
      repo = m2[2];
    } else {
      return { error: 'invalid GitHub repository URL' };
    }
  }
  repo = repo.replace(/\.git$/i, '');
  const seg = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  if (owner === '.' || owner === '..' || !seg.test(owner)) return { error: 'github owner is invalid' };
  if (repo === '.' || repo === '..' || !seg.test(repo)) return { error: 'github repo is invalid' };
  return { owner, repo };
}

/**
 * GitCode 仓库 URL → {owner, repo} 解析（类比 GitHub）。
 * 接受 https://gitcode.com/<owner>/<repo> 或 git@gitcode.com:<owner>/<repo>(.git)
 * 对齐后端 _safe_segment 规则：owner/repo 须匹配 ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$
 * 返回 null + 不合法时返回错误信息。
 */
export function parseGitcodeRepoUrl(value: string): { owner: string; repo: string } | { error: string } {
  const text = value.trim();
  if (!text) return { error: 'gitcode repo url is required' };
  let owner = '';
  let repo = '';
  const m = text.match(/^https?:\/\/(?:[^/]*\.)?gitcode\.com\/([^/]+)\/([^/?#]+)/i);
  if (m) {
    owner = m[1];
    repo = m[2];
  } else {
    const m2 = text.match(/^git@gitcode\.com:([^/]+)\/([^?#]+)$/i);
    if (m2) {
      owner = m2[1];
      repo = m2[2];
    } else {
      return { error: 'invalid GitCode repository URL' };
    }
  }
  repo = repo.replace(/\.git$/i, '');
  const seg = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/;
  if (owner === '.' || owner === '..' || !seg.test(owner)) return { error: 'gitcode owner is invalid' };
  if (repo === '.' || repo === '..' || !seg.test(repo)) return { error: 'gitcode repo is invalid' };
  return { owner, repo };
}

/**
 * 知乎专栏 column_url 前端预校验，对齐后端 _normalize_url + path 校验
 * （openjiuwen/harness/personal_context/config.py 的 zhihu_reader 分支）：
 * - 必须 https
 * - host 为 zhihu.com 或其子域，无 userinfo/password
 * - 无 query/fragment/自定义端口
 * - path 以 /column/ 开头，恰好两段 column/<id>
 *
 * 返回 null 表示通过；否则返回错误信息。
 */
export function validateZhihuColumnUrl(value: string): string | null {
  const text = value.trim();
  if (!text) return 'column_url is required';
  let url: URL;
  try {
    url = new URL(text);
  } catch {
    return 'column_url must be a valid URL';
  }
  if (url.protocol !== 'https:') {
    return 'column_url must be an https URL';
  }
  if (url.username || url.password) {
    return 'column_url must not contain userinfo';
  }
  if (url.port) {
    return 'column_url must not contain a custom port';
  }
  if (url.search || url.hash) {
    return 'column_url must not contain query or fragment';
  }
  const host = url.hostname.toLowerCase();
  if (host !== 'zhihu.com' && !host.endsWith('.zhihu.com')) {
    return 'column_url must point to zhihu.com';
  }
  const parts = url.pathname.split('/').filter(Boolean);
  if (parts.length !== 2 || parts[0].toLowerCase() !== 'column' || !parts[1]) {
    return 'column_url must be a single Zhihu column URL (https://www.zhihu.com/column/<id>)';
  }
  return null;
}
