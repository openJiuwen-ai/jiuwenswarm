// RSI mock 数据：接口未就绪时驱动前端开发。
// 集中在此文件，接口 ready 后整体删除即可，业务层只调 rsiApi.ts，不直接 import 本文件。
// 数据形状严格遵循 types.ts，便于一键替换为真实接口。

import type {
  RsiTaskListItem,
  RsiTaskGetResult,
  RsiTreeGetResult,
  RsiReportGetResult,
  RsiUsageGetResult,
  RsiArtifactDownloadResult,
  RsiArtifactFileGetResult,
  RsiArtifactFilesListResult,
} from './types';

// 延时模拟网络往返，避免 mock 响应过快掩盖 loading 态
const LATENCY = 220;
function delay<T>(value: T, ms = LATENCY): Promise<T> {
  return new Promise((resolve) => setTimeout(() => resolve(value), ms));
}

export const RSI_LOCAL_ARTIFACT_PATH = 'C:/Users/ray_l/.jiuwenswarm/rsi/tasks/package';
const LOCAL_TEST_ARTIFACT_PATH = RSI_LOCAL_ARTIFACT_PATH;

export const rsiMockModelList: Array<{ id: string; name: string; is_free: boolean; provider?: string }> = [
  { id: 'model-qwen-max', name: 'Qwen-Max', is_free: false, provider: 'dashscope' },
  { id: 'model-qwen-plus', name: 'Qwen-Plus', is_free: true, provider: 'dashscope' },
  { id: 'model-deepseek-v3', name: 'DeepSeek-V3', is_free: true, provider: 'deepseek' },
  { id: 'model-glm-4', name: 'GLM-4', is_free: false, provider: 'zhipu' },
];

const mockTasks: RsiTaskListItem[] = [
  {
    task_id: 'rsi-task-001',
    name: 'Harness 基线优化 A',
    scenario: 'HARNESS',
    artifact_type: null,
    status: 'RUNNING',
    created_at: '2026-08-28T10:12:00Z',
  },
  {
    task_id: 'rsi-task-002',
    name: '论文润色 v2',
    scenario: 'ARTIFACT',
    artifact_type: 'PAPER',
    status: 'COMPLETED',
    created_at: '2026-08-25T09:30:00Z',
  },
  {
    task_id: 'rsi-task-003',
    name: '程序优化-工具链',
    scenario: 'ARTIFACT',
    artifact_type: 'PROGRAM',
    status: 'PAUSED',
    created_at: '2026-08-30T14:00:00Z',
  },
  {
    task_id: 'rsi-task-004',
    name: 'Harness 排队演示',
    scenario: 'HARNESS',
    artifact_type: null,
    status: 'QUEUED',
    created_at: '2026-09-02T09:00:00Z',
  },
];

function buildMockTree(): RsiTreeGetResult {
  // 有向树：根 → 多层分支派生，覆盖四种节点状态 + 变更明细
  const mk = (
    id: string,
    iteration: number,
    parentId: string | null,
    type: RsiTreeGetResult['nodes'][number]['type'],
    score: number | null,
    description: string | null,
    changes: RsiTreeGetResult['nodes'][number]['changes'],
    opts: Partial<RsiTreeGetResult['nodes'][number]> = {},
  ): RsiTreeGetResult['nodes'][number] => ({
    node_id: id,
    iteration,
    parent_id: parentId,
    type,
    adopted: type === 'ADOPTED',
    score,
    description,
    snapshot_artifact_id: type === 'ADOPTED' ? `${id}-snap` : null,
    failure_reason: opts.failure_reason ?? null,
    failure_class: opts.failure_class ?? null,
    changes,
    extra: opts.extra ?? null,
  });

  const nodes: RsiTreeGetResult['nodes'] = [
    mk('H-00', 0, null, 'ROOT', 61.5, '基线快照', null),
    // iteration 1
    mk('H-01', 1, 'H-00', 'ADOPTED', 63.1, 'Prompt v2 调优', [
      { group: 'prompt', operation: 'modify', function: 'system', target: 'system_prompt', summary: '精简系统提示词' },
    ]),
    mk(
      'H-02',
      1,
      'H-00',
      'REJECTED',
      60.2,
      'Skill 调整未通过',
      [
        {
          group: 'skill',
          operation: 'add',
          function: 'search',
          target: 'web_search',
          summary: '新增搜索技能但评测下降',
        },
      ],
      {
        failure_reason: '评测分数低于父节点',
        failure_class: 'score_drop',
        extra: { potential_score: 58.4, other_score: 57.1, stability_score: 56.9, cost_score: 55.2 },
      },
    ),
    mk(
      'H-03',
      1,
      'H-00',
      'PRUNED',
      null,
      '门控剪枝',
      [{ group: 'tool', operation: 'modify', function: 'code_run', target: 'sandbox', summary: '安全门未通过' }],
      { failure_reason: 'removed gate 未通过' },
    ),
    // iteration 2
    mk('H-04', 2, 'H-01', 'ADOPTED', 64.8, 'Rail v1 约束增强', [
      { group: 'rail', operation: 'add', function: 'safety', target: 'output_filter', summary: '增加输出过滤护栏' },
    ]),
    mk(
      'H-05',
      2,
      'H-01',
      'REJECTED',
      63.9,
      '工具替换效果不佳',
      [{ group: 'tool', operation: 'modify', function: 'code_run', target: 'runner', summary: '更换运行器' }],
      { failure_reason: '组合评测未达阈值' },
    ),
    // iteration 3
    mk('H-06', 3, 'H-04', 'ADOPTED', 65.5, 'Prompt v3 + Tool 调整', [
      { group: 'prompt', operation: 'modify', function: 'system', target: 'system_prompt', summary: '强化角色设定' },
      { group: 'tool', operation: 'add', function: 'memory', target: 'recall', summary: '增加记忆召回工具' },
    ]),
    mk(
      'H-07',
      3,
      'H-04',
      'PROVISIONAL',
      null,
      '【正在分析实验】节点默认最小宽度180px，开发评估下能不能根据内容适配，最大宽度280px，并且内容支持多行展示。评测中…',
      [{ group: 'skill', operation: 'modify', function: 'plan', target: 'planner', summary: '调整规划技能' }],
    ),
    // iteration 4
    mk(
      'H-08',
      4,
      'H-06',
      'ADOPTED',
      66.7,
      'Prompt v4 → v5 + Rail v1 → v2',
      [
        { group: 'prompt', operation: 'modify', function: 'system', target: 'system_prompt', summary: '压缩提示词' },
        { group: 'rail', operation: 'modify', function: 'safety', target: 'output_filter', summary: '收紧护栏阈值' },
      ],
      {
        extra: {
          potential_score: 65.8,
          other_score: 64.2,
          efficiency_score: 63.5,
          artifacts: [{ path: LOCAL_TEST_ARTIFACT_PATH }],
        },
      },
    ),
    mk(
      'H-09',
      4,
      'H-06',
      'REJECTED',
      66.1,
      '调整未超过父节点',
      [{ group: 'tool', operation: 'modify', function: 'code_run', target: 'runner', summary: '微调运行参数' }],
      { failure_reason: '得分未超过 H-06' },
    ),
  ];

  return { nodes, depth: 4, iteration: 4 };
}

const mockTree = buildMockTree();

const mockTaskGet: Record<string, RsiTaskGetResult> = {
  'rsi-task-001': {
    task_id: 'rsi-task-001',
    name: 'Harness 基线优化 A',
    scenario: 'HARNESS',
    artifact_type: null,
    status: 'RUNNING',
    config: {
      model: { optimizer: 'model-qwen-max', tester: 'model-qwen-plus' },
      input_file: 'C:/data/dataset.json',
      max_iterations: 5,
      optimization_instruction: null,
      artifact_path: null,
      web_proxy_configured: false,
    },
    progress: { iteration: 128, total_iterations: 200, score: 66.7, baseline: 61.5 },
    best_artifact: { artifact_id: 'H-08-snap', name: '快照H-08', adopted: true },
    usage: {
      tokens: { input: 980000, output: 250000, cache_hit: 120000 },
      cost_estimate: 23.45,
      call_count: 36,
    },
  },
  'rsi-task-002': {
    task_id: 'rsi-task-002',
    name: '论文润色 v2',
    scenario: 'ARTIFACT',
    artifact_type: 'PAPER',
    status: 'COMPLETED',
    config: {
      model: { optimizer: 'model-deepseek-v3', tester: null },
      input_file: null,
      max_iterations: 5,
      optimization_instruction: '增强逻辑连贯性与术语一致性',
      artifact_path: 'D:/docs/paper_v1.pdf',
      web_proxy_configured: true,
    },
    progress: { iteration: 5, total_iterations: 5, score: 88.2, baseline: 79.0 },
    best_artifact: { artifact_id: 'P-05-snap', name: '论文 v5', adopted: true },
    usage: {
      tokens: { input: 320000, output: 180000, cache_hit: 60000 },
      cost_estimate: 18.6,
      call_count: 20,
    },
  },
  'rsi-task-003': {
    task_id: 'rsi-task-003',
    name: '程序优化-工具链',
    scenario: 'ARTIFACT',
    artifact_type: 'PROGRAM',
    status: 'PAUSED',
    config: {
      model: { optimizer: 'model-glm-4', tester: null },
      input_file: null,
      max_iterations: 3,
      optimization_instruction: null,
      artifact_path: 'D:/projects/toolchain/main.py',
      web_proxy_configured: false,
    },
    progress: { iteration: 2, total_iterations: 3, score: 72.4, baseline: 68.0 },
    best_artifact: null,
    usage: {
      tokens: { input: 410000, output: 96000, cache_hit: 30000 },
      cost_estimate: 9.8,
      call_count: 12,
    },
  },
  'rsi-task-004': {
    task_id: 'rsi-task-004',
    name: 'Harness 排队演示',
    scenario: 'HARNESS',
    artifact_type: null,
    status: 'QUEUED',
    config: {
      model: { optimizer: 'model-qwen-max', tester: 'model-qwen-plus' },
      input_file: 'C:/data/dataset.json',
      max_iterations: 5,
      optimization_instruction: null,
      artifact_path: null,
      web_proxy_configured: false,
    },
    progress: { iteration: 0, total_iterations: 5, score: null, baseline: null },
    best_artifact: null,
    usage: null,
  },
};

const mockReport: Record<string, RsiReportGetResult> = {
  'rsi-task-001': {
    status: 'RUNNING',
    best_score: 66.7,
    baseline: 61.5,
    metrics: { eval_passed: 36, eval_total: 40, pruned_count: 2, iterations: 4 },
    usage: mockTaskGet['rsi-task-001'].usage ?? null,
    best_artifact: mockTaskGet['rsi-task-001'].best_artifact,
    report_summary: '前 4 轮迭代中 Prompt 与 Rail 协同优化，分数由 61.5 提升至 66.7，组合评测 36/40 通过。',
    markdown: null,
  },
  'rsi-task-002': {
    status: 'COMPLETED',
    best_score: 88.2,
    baseline: 79.0,
    metrics: { eval_passed: 19, eval_total: 20, pruned_count: null, iterations: 5 },
    usage: mockTaskGet['rsi-task-002'].usage ?? null,
    best_artifact: mockTaskGet['rsi-task-002'].best_artifact,
    report_summary: '论文经 5 轮润色，逻辑连贯性与术语一致性显著提升。',
    markdown: null,
  },
  'rsi-task-003': {
    status: 'PAUSED',
    best_score: 72.4,
    baseline: 68.0,
    metrics: { eval_passed: 7, eval_total: 10, pruned_count: null, iterations: 200 },
    usage: mockTaskGet['rsi-task-003'].usage ?? null,
    best_artifact: null,
    report_summary: '已暂停，前 2 轮迭代完成。',
    markdown: null,
  },
};

const mockUsage: Record<string, RsiUsageGetResult> = {
  'rsi-task-001': {
    usage: mockTaskGet['rsi-task-001'].usage!,
    per_iteration: [
      {
        iteration: 1,
        usage: { tokens: { input: 200000, output: 50000, cache_hit: 20000 }, cost_estimate: 5.1, call_count: 8 },
      },
      {
        iteration: 2,
        usage: { tokens: { input: 260000, output: 60000, cache_hit: 30000 }, cost_estimate: 6.3, call_count: 9 },
      },
      {
        iteration: 3,
        usage: { tokens: { input: 280000, output: 70000, cache_hit: 40000 }, cost_estimate: 6.8, call_count: 10 },
      },
      {
        iteration: 4,
        usage: { tokens: { input: 240000, output: 70000, cache_hit: 30000 }, cost_estimate: 5.25, call_count: 9 },
      },
    ],
    usage_by_node: {
      'H-01': { tokens: { input: 200000, output: 50000, cache_hit: 20000 }, cost_estimate: 5.1, call_count: 8 },
      'H-04': { tokens: { input: 260000, output: 60000, cache_hit: 30000 }, cost_estimate: 6.3, call_count: 9 },
      'H-06': { tokens: { input: 280000, output: 70000, cache_hit: 40000 }, cost_estimate: 6.8, call_count: 10 },
      'H-08': { tokens: { input: 240000, output: 70000, cache_hit: 30000 }, cost_estimate: 5.25, call_count: 9 },
    },
  },
};

function mockArtifactRoot(taskId: string, artifactId = 'best'): string {
  return `mock://rsi/${encodeURIComponent(taskId)}/${encodeURIComponent(artifactId)}`;
}

function mockArtifactFilesList(taskId: string, path: string): RsiArtifactFilesListResult {
  const root = path.trim().replace(/\/+$/, '') || mockArtifactRoot(taskId);
  const readmePath = `${root}/README.md`;
  const nodePath = `${root}/node.json`;
  const diffDirectory = `${root}/changes`;
  const diffPath = `${diffDirectory}/iteration-004.diff`;
  const tracePath = `${root}/agent_trace.jsonl`;
  const readme = 'This is a deterministic mock RSI artifact.\n';
  const node = JSON.stringify({ task_id: taskId, root, status: 'adopted' }, null, 2);
  const diff = '# mock artifact change\n+ generated node snapshot\n';
  const trace = '{"event":"artifact_ready","mock":true}\n';
  return {
    root,
    initial_path: readmePath,
    files: [
      { name: 'README.md', path: readmePath, isDirectory: false, size: readme.length, type: 'text/markdown' },
      { name: 'node.json', path: nodePath, isDirectory: false, size: node.length, type: 'application/json' },
      { name: 'changes', path: diffDirectory, isDirectory: true, size: 0, type: 'application/octet-stream' },
      { name: 'iteration-004.diff', path: diffPath, isDirectory: false, size: diff.length, type: 'text/plain' },
      { name: 'agent_trace.jsonl', path: tracePath, isDirectory: false, size: trace.length, type: 'application/x-ndjson' },
    ],
  };
}

function mockArtifactFilesGet(taskId: string, path: string): RsiArtifactFileGetResult {
  const name = path.split(/[\\/]/).pop()?.toLowerCase() ?? '';
  let content = 'This is a deterministic mock RSI artifact.\n';
  let type = 'text/markdown';
  if (name === 'node.json') {
    content = JSON.stringify({ task_id: taskId, path, status: 'adopted' }, null, 2);
    type = 'application/json';
  } else if (name.endsWith('.diff')) {
    content = '# mock artifact change\n+ generated node snapshot\n';
    type = 'text/plain';
  } else if (name.endsWith('.jsonl')) {
    content = '{"event":"artifact_ready","mock":true}\n';
    type = 'application/x-ndjson';
  }
  return { path, name: path.split(/[\\/]/).pop() || 'artifact', size: content.length, type, encoding: 'text', content };
}

function mockArtifactDownload(taskId: string, artifactId?: string): RsiArtifactDownloadResult {
  const safeArtifactId = (artifactId || 'best').replace(/[^A-Za-z0-9_.-]+/g, '-');
  return {
    path: mockArtifactRoot(taskId, artifactId || 'best'),
    kind: 'artifact_package',
    is_best: true,
    is_directory: true,
    filename: `mock-${taskId}-${safeArtifactId}`,
  };
}

export const rsiMock = {
  delay,
  modelList: rsiMockModelList,
  tasks: mockTasks,
  taskGet: (taskId: string) => mockTaskGet[taskId],
  tree: (taskId: string) => (taskId === 'rsi-task-004' ? { nodes: [], depth: 0, iteration: 0 } : mockTree), // 排队中的实验暂无演进树；其余复用同一份 mock 树
  report: (taskId: string) => mockReport[taskId],
  usage: (taskId: string) => mockUsage[taskId],
  artifactDownload: mockArtifactDownload,
  artifactFilesList: mockArtifactFilesList,
  artifactFilesGet: mockArtifactFilesGet,
};
