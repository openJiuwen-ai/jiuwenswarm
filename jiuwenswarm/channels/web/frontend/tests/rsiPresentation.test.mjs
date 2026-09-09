import assert from 'node:assert/strict';
import test from 'node:test';
import {
  formatArtifactScore,
  nodeChangeDisplayLabel,
  nodeScoreLines,
  nodeStageLabel,
  nodeStageLocalizedLabel,
  nodeStageSpec,
  presentRsiNode,
  scoreScale,
  actionsForStatus,
} from '../node_modules/.cache/rsi-presentation/rsiPresentation.mjs';

const context = (scenario, artifactType, nodes, taskRunning = false) => ({
  scenario,
  artifactType,
  allNodes: nodes,
  taskRunning,
});

test('paper nodes use a stable version title and expose the stage separately', () => {
  const root = {
    node_id: 'ROOT',
    iteration: 0,
    parent_id: null,
    type: 'ROOT',
    adopted: true,
    score: null,
    description: 'Uploaded starting paper (ingestion not yet wired in).',
    changes: [],
    extra: {},
  };
  const candidate = {
    node_id: 'artifact:task:node:1',
    iteration: 1,
    parent_id: 'ROOT',
    type: 'PROVISIONAL',
    adopted: false,
    score: null,
    description: '正在撰写论文',
    failure_reason: null,
    failure_class: null,
    changes: [{ group: 'PAPER', operation: 'generate', summary: 'Generated a new paper version.' }],
    extra: {
      paper: { round_index: 1, attempt: 1, outcome: 'pending' },
      stage: { id: 'reporting', name: '正在撰写论文' },
    },
  };
  const presentation = presentRsiNode(candidate, context('ARTIFACT', 'PAPER', [root, candidate], true));
  const rootPresentation = presentRsiNode(root, context('ARTIFACT', 'PAPER', [root, candidate], true));
  assert.equal(presentation.title, '第 1 轮 · 论文候选');
  assert.equal(presentation.lifecycle, 'generating');
  assert.equal(presentation.stageLabel, '撰写中');
  assert.equal(presentation.summary, '生成新的论文版本');
  assert.equal(nodeStageLabel(candidate), '撰写中');
  assert.equal(rootPresentation.summary, '已上传起始论文');
});

test('baseline nodes use a semantic subtitle and accept legacy string stages', () => {
  const root = {
    node_id: 'ROOT',
    iteration: 0,
    parent_id: null,
    type: 'ROOT',
    adopted: true,
    description: 'paper optimization root',
    changes: [],
    extra: { stage: 'research' },
  };
  const presentation = presentRsiNode(root, context('ARTIFACT', 'PAPER', [root]));

  assert.equal(presentation.title, '基线论文');
  assert.equal(presentation.subtitle, '优化起点');
  assert.equal(presentation.stageLabel, '调研中');
  assert.equal(presentation.summary, '从基线开始生成论文');
});

test('in-flight artifact generation is labeled as generation, not evaluation', () => {
  const node = {
    node_id: 'rsi:node:2',
    parent_id: 'ROOT',
    type: 'PROVISIONAL',
    iteration: 2,
    description: '正在撰写论文',
    extra: { stage: 'paper_writing' },
  };

  const presentation = presentRsiNode(
    node,
    context(
      'ARTIFACT',
      'PAPER',
      [
        {
          node_id: 'ROOT',
          iteration: 0,
          parent_id: null,
          type: 'ROOT',
          adopted: true,
          changes: [],
          extra: {},
        },
        node,
      ],
      true,
    ),
  );

  assert.equal(presentation.lifecycle, 'generating');
  assert.equal(presentation.stageLabel, '撰写中');
  assert.equal(presentation.runtimeKind, 'evaluating');
  assert.equal(presentation.runtimeLabel, '生成中');
});

test('paper score_overall is rendered and rejected reason is human-readable', () => {
  const parent = {
    node_id: 'paper-parent',
    iteration: 1,
    parent_id: 'ROOT',
    type: 'ADOPTED',
    adopted: true,
    score: null,
    description: '上一版论文',
    changes: [],
    extra: { paper: { round_index: 1, attempt: 1, outcome: 'success', score_overall: 0.82 } },
  };
  const rejected = {
    node_id: 'paper-rejected',
    iteration: 2,
    parent_id: 'paper-parent',
    type: 'REJECTED',
    adopted: false,
    score: null,
    description: '完成论文版本',
    failure_reason: 'score 0.79 did not exceed parent score 0.82',
    failure_class: 'rejected_by_score',
    changes: [{ group: 'paper', operation: 'modify', summary: '新增实验小节' }],
    extra: { paper: { round_index: 2, attempt: 1, outcome: 'rejected', score_overall: 0.79 } },
  };
  const presentation = presentRsiNode(rejected, context('ARTIFACT', 'PAPER', [parent, rejected]));
  assert.equal(presentation.title, '第 2 轮 · 论文候选');
  assert.equal(presentation.lifecycle, 'rejected');
  assert.equal(presentation.reasonLabel, '得分未超过父节点');
  assert.equal(presentation.score, 0.79);
  assert.equal(presentation.parentScore, 0.82);
  assert.deepEqual(nodeScoreLines(rejected)[0], { value: '0.8', label: '分数' });
});

test('paper tasks can pause but do not expose an unsupported resume action', () => {
  assert.deepEqual(actionsForStatus('RUNNING', 'ARTIFACT', false, null, 'PAPER'), [
    'config',
    'delete',
    'pause',
  ]);
  assert.deepEqual(actionsForStatus('PAUSED', 'ARTIFACT', false, null, 'PAPER'), [
    'config',
    'delete',
    'stop',
  ]);
  assert.deepEqual(actionsForStatus('PAUSED', 'ARTIFACT', false, null, 'PROGRAM'), [
    'config',
    'delete',
    'resume',
  ]);
});

test('parallel program candidates get attempt numbering without exposing provider ids', () => {
  const nodes = [
    {
      node_id: 'artifact:task:root',
      iteration: 0,
      parent_id: null,
      type: 'ROOT',
      adopted: true,
      score: 0.5,
      changes: [],
      extra: { program: {} },
    },
    {
      node_id: 'artifact:task:attempt:1:a',
      iteration: 1,
      parent_id: 'ROOT',
      type: 'PROVISIONAL',
      adopted: false,
      score: null,
      changes: [],
      extra: { program: { logical_kind: 'pending' } },
    },
    {
      node_id: 'artifact:task:attempt:1:b',
      iteration: 1,
      parent_id: 'ROOT',
      type: 'PROVISIONAL',
      adopted: false,
      score: null,
      changes: [],
      extra: { program: { logical_kind: 'pending' } },
    },
    {
      node_id: 'artifact:task:attempt:1:c',
      iteration: 1,
      parent_id: 'ROOT',
      type: 'PROVISIONAL',
      adopted: false,
      score: null,
      changes: [],
      extra: { program: { logical_kind: 'pending' } },
    },
  ];
  const presentation = presentRsiNode(nodes[1], context('ARTIFACT', 'PROGRAM', nodes, true));
  assert.match(presentation.title, /^第 1 轮 · 程序候选 [1-3]\/3$/);
  assert.equal(presentation.attempt.total, 3);
  assert.equal(presentation.lifecycle, 'generating');
  assert.equal(nodeChangeDisplayLabel({ group: 'program', operation: 'modify' }), '程序逻辑 · 调整');
});

test('runtime failures are separated from score-based rejection', () => {
  const failed = {
    node_id: 'paper-failed',
    iteration: 3,
    parent_id: 'ROOT',
    type: 'REJECTED',
    adopted: false,
    score: null,
    description: null,
    failure_reason: 'manager decision failed after 3 attempts',
    failure_class: 'pipeline_failed',
    changes: [],
    extra: { paper: { round_index: 3, attempt: 1, outcome: 'failed' } },
  };
  const presentation = presentRsiNode(failed, context('ARTIFACT', 'PAPER', [failed], false));
  assert.equal(presentation.title, '第 3 轮 · 论文尝试');
  assert.equal(presentation.lifecycle, 'failed');
  assert.equal(presentation.reasonLabel, '管理器决策失败');
});

test('pruned paper nodes expose a concise user-facing reason', () => {
  const pruned = {
    node_id: 'paper-pruned',
    iteration: 4,
    parent_id: 'ROOT',
    type: 'PRUNED',
    adopted: false,
    score: null,
    description: null,
    failure_reason: '资料获取质量不佳，已剪枝。',
    failure_class: 'pipeline_failed',
    changes: [],
    extra: { paper: { round_index: 4, attempt: 1, outcome: 'failed' } },
  };

  const presentation = presentRsiNode(pruned, context('ARTIFACT', 'PAPER', [pruned], false));
  assert.equal(presentation.lifecycle, 'pruned');
  assert.equal(presentation.reasonLabel, '资料获取质量不佳，已剪枝。');
  assert.equal(presentation.reasonDetail, null);
});

test('structured harness stage payloads localize by status instead of using the provider name', () => {
  const node = {
    node_id: 'rsi:node:case',
    iteration: 1,
    parent_id: 'ROOT',
    type: 'PROVISIONAL',
    adopted: false,
    score: null,
    description: 'Case 2/5 passed',
    failure_reason: null,
    failure_class: null,
    changes: [],
    extra: {
      stage: {
        id: 'evaluate.case.2',
        name: 'Case 2/5 passed · score 0.85',
        status: 'passed',
        case_index: 2,
        total_cases: 5,
        case_id: 'case-a',
        score: 0.85,
      },
    },
  };

  assert.deepEqual(nodeStageSpec(node), {
    id: 'evaluate.case.2',
    status: 'passed',
    name: 'Case 2/5 passed · score 0.85',
    failedCaseCount: null,
    caseIndex: 2,
    totalCases: 5,
    caseId: 'case-a',
    score: 0.85,
    candidateIndex: null,
    totalCandidates: null,
  });

  const t = (key, options = {}) => {
    if (key === 'rsi.stage.casePassed') {
      return `评测用例 ${options.index}/${options.total} 通过`;
    }
    if (key === 'rsi.stage.scoreSuffix') {
      return ` · 得分 ${options.score}`;
    }
    return null;
  };
  assert.equal(
    nodeStageLocalizedLabel(node, t),
    '评测用例 2/5 通过 · 得分 0.85',
  );

  const presentation = presentRsiNode(node, context('HARNESS', null, [node], true));
  assert.equal(presentation.lifecycle, 'evaluating');
  assert.equal(presentation.runtimeLabel, '评测中');
});

test('parallel evaluation displays completed count rather than last finished case index', () => {
  const node = {
    node_id: 'epoch-001', iteration: 1, parent_id: 'ROOT', type: 'PROVISIONAL', adopted: false,
    score: null, changes: [], extra: { stage: {
      id: 'evaluate.parallel', name: 'Cases 1/5 completed', status: 'running',
      case_index: 4, case_id: 'fourth', total_cases: 5, completed_cases: 1, score: 1,
    } },
  };
  const presentation = presentRsiNode(node, context('HARNESS', null, [node], true));
  assert.equal(presentation.lifecycle, 'evaluating');
  assert.equal(presentation.stageLabel, 'Cases 1/5 completed');
  const label = nodeStageLocalizedLabel(node, () => 'wrong case index') ?? presentation.stageLabel;
  assert.equal(label, 'Cases 1/5 completed');
});

test('program scores are shown out of 100; other scenarios are unchanged', () => {
  // 引擎给的是 [0,1] 的归一化分数，在 0.5 附近以千分位变化：论文那套 1 位原值
  // 会把相邻两代抹成同一个数，所以程序演进按百分制显示。
  const node = {
    node_id: 'N1',
    iteration: 3,
    parent_id: 'ROOT',
    type: 'ADOPTED',
    score: 0.5524531,
    extra: { potential_score: 0.5001 },
  };

  assert.deepEqual(nodeScoreLines(node, 'PROGRAM'), [
    { value: '55.2', label: '分数' },
    { value: '50.0', label: '潜力分' },
  ]);
  assert.deepEqual(nodeScoreLines(node, 'PAPER')[0], { value: '0.6', label: '分数' });
  assert.deepEqual(nodeScoreLines(node)[0], { value: '0.6', label: '分数' });

  assert.equal(formatArtifactScore(0.5524531, 'PROGRAM'), '55.2');
  assert.equal(formatArtifactScore(0.5, 'PROGRAM'), '50.0');
  assert.equal(formatArtifactScore(0.79, 'PAPER'), '0.8');
  assert.equal(formatArtifactScore(0.79, undefined), '0.8');
  assert.equal(formatArtifactScore(null, 'PROGRAM'), '--');
  assert.equal(scoreScale('PROGRAM'), 100);
  assert.equal(scoreScale('PAPER'), 1);
});
