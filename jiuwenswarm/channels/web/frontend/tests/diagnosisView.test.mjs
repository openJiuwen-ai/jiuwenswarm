import assert from 'node:assert/strict';
import test from 'node:test';

import {
  artifactSuggestion,
  artifactSuggestionItems,
  causeNodeLabel,
  commentPoints,
  diagnosisTarget,
  evidenceNodeLabel,
  filterRecords,
  findingCounts,
  findingItems,
  formatElapsed,
  formatRecordTime,
  formatTimeWindow,
  isStageExpanded,
  legacyArtifactInstruction,
  looksLikePath,
  processSummary,
  recordIconKind,
  recordLabel,
  rewardOverview,
  rewardTier,
  signalEvidenceBody,
  signalEvidenceText,
  signalTier,
  splitLeadParagraph,
  splitRewardSignalText,
  splitStageOutput,
  stageOutputBlocks,
  stageViews,
  stripLeadingHeading,
  suggestionsState,
  truncateText,
  skillSuggestionText,
  skillBatchFillText,
  unixNanoToMs,
  usesLegacyFindings,
} from '../node_modules/.cache/trajectory-diagnosis/diagnosisView.mjs';

const stage = (stageId, status) => ({ stage_id: stageId, title: stageId, status, output: '' });
const nano = ms => `${BigInt(ms) * 1_000_000n}`;

test('a running diagnosis lists waiting stages for what has not started', () => {
  const views = stageViews([stage('task_parse', 'completed')], true);
  assert.deepEqual(
    views.map(view => [view.stageId, view.status, view.stage === null]),
    [
      ['task_parse', 'completed', false],
      ['diagnose', 'pending', true],
      ['attribute', 'pending', true],
    ],
  );
});

test('a running diagnosis keeps stages outside the plan after the planned ones', () => {
  const views = stageViews([stage('evidence', 'completed'), stage('diagnose', 'running')], true);
  assert.deepEqual(
    views.map(view => [view.stageId, view.status]),
    [['task_parse', 'pending'], ['diagnose', 'running'], ['attribute', 'pending'], ['evidence', 'completed']],
  );
});

test('a finished diagnosis lists only the stages the engine reported', () => {
  const views = stageViews([stage('task_parse', 'completed'), stage('diagnose', 'failed')], false);
  assert.deepEqual(views.map(view => view.stageId), ['task_parse', 'diagnose']);
  assert.deepEqual(stageViews([], false), []);
});

test('while the diagnosis runs every stage with a body is open', () => {
  assert.equal(isStageExpanded('completed', undefined, true), true);
  assert.equal(isStageExpanded('running', undefined, true), true);
  assert.equal(isStageExpanded('failed', undefined, true), true);
  assert.equal(isStageExpanded('skipped', undefined, true), true);
});

test('once the diagnosis is over completed stages fold away while the rest stay open', () => {
  assert.equal(isStageExpanded('completed', undefined, false), false);
  assert.equal(isStageExpanded('failed', undefined, false), true);
  assert.equal(isStageExpanded('skipped', undefined, false), true);
});

test('a waiting stage never opens and a user choice beats the default', () => {
  assert.equal(isStageExpanded('pending', undefined, true), false);
  assert.equal(isStageExpanded('pending', true, true), false);
  assert.equal(isStageExpanded('completed', true, false), true);
  assert.equal(isStageExpanded('running', false, true), false);
  assert.equal(isStageExpanded('completed', false, true), false);
});

test('record icons separate running, completed and everything else', () => {
  assert.equal(recordIconKind('running'), 'running');
  assert.equal(recordIconKind('completed'), 'completed');
  for (const status of ['failed', 'cancelled', 'interrupted']) {
    assert.equal(recordIconKind(status), 'failed');
  }
});

test('record time is shown in local time and tolerates bad input', () => {
  const date = new Date(2026, 9, 5, 8, 4, 9);
  assert.equal(formatRecordTime(date.toISOString()), '2026-10-05 08:04:09');
  assert.equal(formatRecordTime(undefined), '');
  assert.equal(formatRecordTime(''), '');
  assert.equal(formatRecordTime('not a date'), '');
});

test('truncateText caps the display at the limit, ellipsis included', () => {
  assert.equal(truncateText('abc', 5), 'abc');
  assert.equal(truncateText('  abc  ', 5), 'abc');
  const clipped = truncateText('x'.repeat(600), 500);
  assert.equal(clipped.length, 500);
  assert.ok(clipped.endsWith('…'));
});

test('a record row prefers the trajectory summary, then the legacy headline, then the scope label', () => {
  assert.equal(
    recordLabel({ trajectory_summary: 'Ran the tests', headline: 'Tool failed', scope_kind: 'full' }, 'Entire'),
    'Ran the tests',
  );
  assert.equal(recordLabel({ headline: 'Tool failed', scope_kind: 'full' }, 'Entire'), 'Tool failed');
  assert.equal(recordLabel({ headline: '', scope_kind: 'full' }, 'Entire'), 'Entire');
  assert.equal(recordLabel({ scope_kind: 'selection' }, 'Selected'), 'Selected');
});

test('record search needs every word to match the time or the label', () => {
  const at = new Date(2026, 9, 5, 8, 4, 9).toISOString();
  const items = [
    { diagnosis_id: 'a', created_at: at, scope_kind: 'full', headline: 'Tool Timeout' },
    { diagnosis_id: 'b', created_at: new Date(2026, 9, 6, 9, 0, 0).toISOString(), scope_kind: 'selection' },
  ];
  const scopeLabel = kind => (kind === 'full' ? 'Entire trajectory' : 'Selected range');
  assert.deepEqual(filterRecords(items, '', scopeLabel).map(item => item.diagnosis_id), ['a', 'b']);
  assert.deepEqual(filterRecords(items, 'timeout', scopeLabel).map(item => item.diagnosis_id), ['a']);
  assert.deepEqual(filterRecords(items, '2026-10-06', scopeLabel).map(item => item.diagnosis_id), ['b']);
  assert.deepEqual(filterRecords(items, 'selected 10-06', scopeLabel).map(item => item.diagnosis_id), ['b']);
  assert.deepEqual(filterRecords(items, 'timeout selected', scopeLabel), []);
});

test('unix nanoseconds convert to milliseconds without losing precision', () => {
  assert.equal(unixNanoToMs('1760000000123456789'), 1760000000123);
  assert.equal(unixNanoToMs('0'), 0);
  assert.equal(unixNanoToMs('12.5'), null);
  assert.equal(unixNanoToMs('-1'), null);
  assert.equal(unixNanoToMs(''), null);
});

test('a full-scope record shows the span of its resolved ranges', () => {
  assert.deepEqual(diagnosisTarget({ input: { scope: { kind: 'full' } } }), {
    full: true,
    window: null,
    groups: [],
  });
  const target = diagnosisTarget({
    input: {
      scope: { kind: 'full' },
      resolved_ranges: [
        { subject_id: 'main', start_unix_nano: nano(1_000), end_unix_nano: nano(4_000) },
        { subject_id: 'worker', start_unix_nano: nano(2_000), end_unix_nano: nano(8_000) },
        { subject_id: 'bad', start_unix_nano: 'x', end_unix_nano: nano(9_000) },
      ],
    },
  });
  assert.deepEqual(target, { full: true, window: { startMs: 1_000, endMs: 8_000 }, groups: [] });
});

test('selected ranges with the same window collapse into one group', () => {
  const target = diagnosisTarget({
    input: {
      scope: {
        kind: 'selection',
        ranges: [
          { subject_id: 'main', start_unix_nano: nano(1000), end_unix_nano: nano(2000) },
          { subject_id: 'worker', start_unix_nano: nano(1000), end_unix_nano: nano(2000) },
          { subject_id: 'solo', start_unix_nano: nano(3000), end_unix_nano: nano(4000) },
          { subject_id: 'bad', start_unix_nano: 'x', end_unix_nano: nano(4000) },
        ],
      },
    },
  });
  assert.equal(target.full, false);
  assert.equal(target.window, null);
  assert.deepEqual(target.groups, [
    { startMs: 1000, endMs: 2000, subjectIds: ['main', 'worker'] },
    { startMs: 3000, endMs: 4000, subjectIds: ['solo'] },
  ]);
});

test('a time window repeats the date only across days', () => {
  const start = new Date(2026, 9, 5, 8, 0, 0).getTime();
  assert.equal(formatTimeWindow(start, new Date(2026, 9, 5, 9, 30, 15).getTime()), '2026-10-05 08:00:00 – 09:30:15');
  assert.equal(
    formatTimeWindow(start, new Date(2026, 9, 6, 1, 2, 3).getTime()),
    '2026-10-05 08:00:00 – 2026-10-06 01:02:03',
  );
  assert.equal(formatTimeWindow(Number.NaN, start), '');
});

const cause = (id, extra = {}) => ({
  id,
  category: 'other',
  subject_id: 'main',
  refs: [],
  summary: `summary ${id}`,
  evidence: '',
  ...extra,
});

test('Reward tiers follow the agent-core thresholds', () => {
  assert.equal(rewardTier(100), 'perfect');
  assert.equal(rewardTier(90), 'perfect');
  assert.equal(rewardTier(89.9), 'fair');
  assert.equal(rewardTier(75), 'fair');
  assert.equal(rewardTier(74), 'weak');
  assert.equal(rewardTier(0), 'weak');
});

test('only Reward signals carry a tier, rule signals do not', () => {
  assert.equal(signalTier({ id: 's', source: 'reward', tier: 'weak', score: 60 }), 'weak');
  assert.equal(signalTier({ id: 's', source: 'reward', score: 80 }), 'fair');
  assert.equal(signalTier({ id: 's', source: 'keyword_tool', tier: 'uncertain' }), null);
  assert.equal(signalTier({ id: 's', source: 'reward' }), null);
});

test('the evidence text of a finding comes from the signal, empty when absent', () => {
  assert.equal(signalEvidenceText({ id: 's', source: 'reward', text: ' 要求\n评语 ' }), '要求\n评语');
  assert.equal(signalEvidenceText({ id: 's', source: 'reward' }), '');
  assert.equal(signalEvidenceText(null), '');
});

test('the evidence body drops the requirement already shown as the dimension', () => {
  const signal = { id: 's', source: 'reward', text: '要求 A\n评语 B' };
  assert.equal(signalEvidenceBody(signal, '要求 A'), '评语 B');
  assert.equal(signalEvidenceBody(signal, '要求 B'), '评语 B');
  assert.equal(signalEvidenceBody({ id: 's', source: 'reward', text: '只有要求' }, '只有要求'), '');
  assert.equal(signalEvidenceBody({ id: 's', source: 'reward', text: '原始命中' }, '别的维度'), '原始命中');
  assert.equal(signalEvidenceBody(null, 'x'), '');
});

test('a Reward signal text splits into requirement and comment', () => {
  assert.deepEqual(splitRewardSignalText('测试应当通过\n仍有 2 个用例失败'), {
    requirement: '测试应当通过',
    comment: '仍有 2 个用例失败',
  });
  assert.deepEqual(splitRewardSignalText('只有要求'), { requirement: '只有要求', comment: '' });
  assert.deepEqual(splitRewardSignalText('要求\n评语第一行\n评语第二行'), {
    requirement: '要求',
    comment: '评语第一行\n评语第二行',
  });
});

test('Markdown folds at the first blank line only', () => {
  assert.deepEqual(splitLeadParagraph('**问题**\n一句话\n\n**成因**\n更多'), {
    lead: '**问题**\n一句话',
    rest: '**成因**\n更多',
  });
  assert.deepEqual(splitLeadParagraph('  单段落  '), { lead: '单段落', rest: '' });
  assert.deepEqual(splitLeadParagraph(''), { lead: '', rest: '' });
  assert.deepEqual(splitLeadParagraph('a\n \n\nb'), { lead: 'a', rest: 'b' });
});

test('a heading the suggestion opens with is dropped, but never the only content', () => {
  assert.equal(stripLeadingHeading('## 技能改进意见\n\n- **a**：b\n\n- **c**：d'), '- **a**：b\n\n- **c**：d');
  assert.equal(stripLeadingHeading('- 无标题\n\n## 后面的标题\n内容'), '- 无标题\n\n## 后面的标题\n内容');
  assert.equal(stripLeadingHeading('## 只有标题'), '## 只有标题');
  assert.equal(stripLeadingHeading('  正文  '), '正文');
});

test('the node label keeps gaps and collapses only adjacent steps', () => {
  const ref = (spanId, nodeId) => ({ trace_id: 't', span_id: spanId, subject_id: 'main', node_id: nodeId });
  assert.equal(
    evidenceNodeLabel({ evidence: [ref('1', 'main:23'), ref('2', 'main:30'), ref('3', 'main:23')] }),
    'main:23; main:30',
  );
  assert.equal(
    evidenceNodeLabel({ evidence: [ref('1', 'main:16'), ref('2', 'main:17'), ref('3', 'main:15')] }),
    'main:15 → main:17',
  );
  assert.equal(
    evidenceNodeLabel({
      evidence: [ref('1', 'main:20'), ref('2', 'main:18'), ref('3', 'main:19'), ref('4', 'main:15')],
    }),
    'main:15; main:18 → main:20',
  );
  assert.equal(evidenceNodeLabel({ evidence: [ref('1', 'main:14')] }), 'main:14');
  assert.equal(evidenceNodeLabel({ evidence: [] }), '');
  const team = 'team-member:web_1:jiuwen_team_web_1:team-leader';
  assert.equal(evidenceNodeLabel({ evidence: [ref('1', `${team}:14`)] }), 'team-leader:14');
  assert.equal(
    evidenceNodeLabel({
      evidence: [ref('1', `${team}:16`), ref('2', `${team}:14`), ref('3', `${team}:15`)],
    }),
    'team-leader:14 → team-leader:16',
  );
});

test('findingCounts separates reward causes, skill suggestions and experience verdicts', () => {
  assert.deepEqual(
    findingCounts({
      reward_failure_causes: [{}, {}],
      evolution_judge: [{ category: 'skill' }, { category: 'skill' }, { category: 'experience' }],
    }),
    { causes: 2, skill: 2, experience: 1, legacy: false },
  );
  assert.deepEqual(findingCounts({ reward_failure_causes: [], evolution_judge: [] }), {
    causes: 0,
    skill: 0,
    experience: 0,
    legacy: false,
  });
  assert.deepEqual(
    findingCounts({ root_causes: [cause('a'), cause('b', { category: 'ttse_fail' })], reward_failure_causes: [], evolution_judge: [] }),
    { causes: 1, skill: 0, experience: 1, legacy: true },
  );
});

test('findingItems puts artifact causes first, then skill, then experience', () => {
  const items = findingItems({
    reward_failure_causes: [{ id: 'rfc-1' }, { id: 'rfc-2' }],
    evolution_judge: [
      { id: 'evo-exp', category: 'experience' },
      { id: 'evo-skill', category: 'skill' },
    ],
  });
  assert.deepEqual(items.map(item => [item.id, item.kind]), [
    ['rfc-1', 'artifact'],
    ['rfc-2', 'artifact'],
    ['evo-skill', 'skill'],
    ['evo-exp', 'experience'],
  ]);
  assert.equal(items[0].cause.id, 'rfc-1');
  assert.equal(items[2].judge.id, 'evo-skill');
});

test('legacy records are detected when only root causes exist', () => {
  assert.equal(usesLegacyFindings({ reward_failure_causes: [], evolution_judge: [], root_causes: [cause('a')] }), true);
  assert.equal(
    usesLegacyFindings({ reward_failure_causes: [{}], evolution_judge: [], root_causes: [cause('a')] }),
    false,
  );
});

test('artifactSuggestion joins cause suggestions, or uses the experimental whole block', () => {
  const record = {
    reward_failure_causes: [
      { suggestion: ['first', ' '] },
      { suggestion: ['second'] },
    ],
    artifact_improvement: 'legacy block',
  };
  assert.equal(artifactSuggestion(record, false), '1. first\n2. second');
  assert.equal(artifactSuggestion(record, true), 'legacy block');
});

test('a legacy artifact fix has its own outlet and never doubles the new card', () => {
  const legacy = { reward_failure_causes: [], evolution_judge: [], suggestions: { artifact_fix: { instruction: 'old' } } };
  assert.equal(artifactSuggestion(legacy, false), '');
  assert.equal(legacyArtifactInstruction(legacy), 'old');
  const current = {
    reward_failure_causes: [{ suggestion: ['new'] }],
    evolution_judge: [],
    suggestions: { artifact_fix: { instruction: 'old' } },
  };
  assert.equal(legacyArtifactInstruction(current), '');
});

test('artifactSuggestionItems keeps cause order and the full attribution fields', () => {
  const items = artifactSuggestionItems({
    reward_failure_causes: [
      {
        id: 'rfc-1',
        dimension: '正确性',
        summary: '问题一',
        evidence: [{ trace_id: 't', span_id: 's', subject_id: 'main', node_id: 'main:1' }],
        suggestion: ['先修 A', ' ', '再修 B'],
        signal: { id: 'diag-1', source: 'reward', score: 60 },
      },
      { id: 'rfc-2', dimension: '完整性', summary: '问题二', evidence: [], suggestion: [] },
    ],
  });
  assert.deepEqual(items.map(item => item.id), ['rfc-1', 'rfc-2']);
  assert.equal(items[0].text, '先修 A\n\n再修 B');
  assert.deepEqual(items[0].parts, ['先修 A', '再修 B']);
  assert.equal(items[0].dimension, '正确性');
  assert.equal(items[0].summary, '问题一');
  assert.equal(items[0].signal.id, 'diag-1');
  assert.equal(items[1].text, '问题二'); // defensive: an empty suggestion shows the summary
});

test('skillSuggestionText keeps one block and numbers several entries', () => {
  assert.equal(skillSuggestionText({ suggestion: ['# a\n\n- b'] }), '# a\n\n- b');
  assert.equal(skillSuggestionText({ suggestion: [] }), '');
  assert.equal(skillSuggestionText({ suggestion: ['先改提示词', '再加自检'] }), '1. 先改提示词\n\n2. 再加自检');
});

test('the node label joins distinct node ids with an arrow', () => {
  const ref = nodeId => ({ trace_id: 't', span_id: 's', subject_id: 'main', ...(nodeId === undefined ? {} : { node_id: nodeId }) });
  assert.equal(causeNodeLabel({ refs: [ref('main:23'), ref('main:30')] }), 'main:23; main:30');
  assert.equal(causeNodeLabel({ refs: [ref('main:23'), ref('main:23')] }), 'main:23');
  assert.equal(causeNodeLabel({ refs: [ref(undefined)] }), '');
  assert.equal(causeNodeLabel({ refs: [] }), '');
});

test('stage time reads in ms, seconds or minutes', () => {
  assert.equal(formatElapsed(undefined), '');
  assert.equal(formatElapsed(-1), '');
  assert.equal(formatElapsed(850), '850ms');
  assert.equal(formatElapsed(20_400), '20.4s');
  assert.equal(formatElapsed(65_000), '1m 5s');
  assert.equal(formatElapsed(120_000), '2m 0s');
});

test('a Skill key that reads as a path is recognised', () => {
  assert.equal(looksLikePath('baoyu-image-gen'), false);
  assert.equal(looksLikePath('skills/demo/SKILL.md'), true);
  assert.equal(looksLikePath('C:\\skills\\demo'), true);
  assert.equal(looksLikePath('README.MD'), true);
});

test('the suggestion card tells running, unfinished and finished apart', () => {
  assert.equal(suggestionsState('running'), 'pending');
  assert.equal(suggestionsState('completed'), 'ready');
  for (const status of ['failed', 'cancelled', 'interrupted']) {
    assert.equal(suggestionsState(status), 'unfinished');
  }
});

test('model request lines are split from the rest of a stage output', () => {
  const output = [
    'llm-1 · task_parse · 请求中 · m',
    'llm-1 · task_parse · iter 1 · 成功 · m',
    '任务描述：装好依赖',
    'llm-2 · task_parse · 成功 · m',
    '消息产物 main:0：先看 README',
  ].join('\n');
  const view = splitStageOutput(output);
  assert.equal(view.requestCount, 2);
  assert.equal(view.requestLines.length, 3);
  assert.equal(view.body, '任务描述：装好依赖\n消息产物 main:0：先看 README');
  assert.deepEqual(splitStageOutput(''), { body: '', requestLines: [], requestCount: 0 });
});

test('one chosen skill fills as itself and several are headed by the skill name', () => {
  const image = { id: 'evo-1', target: 'baoyu-image-gen', suggestion: ['## 交付前质量校验\n\n- 先自检'] };
  const other = { id: 'evo-2', target: 'writer', suggestion: ['补上引用'] };
  const prefix = '根据以下优化建议对相应skill进行演进：';
  assert.equal(skillBatchFillText([image], prefix), `${prefix}\n\n## 交付前质量校验\n\n- 先自检`);
  assert.equal(
    skillBatchFillText([image, other], prefix),
    [
      prefix,
      '',
      '## baoyu-image-gen',
      '',
      '## 交付前质量校验',
      '',
      '- 先自检',
      '',
      '## writer',
      '',
      '补上引用',
    ].join('\n'),
  );
  assert.equal(skillBatchFillText([], prefix), prefix);
});

test('a running stage keeps arrival order and a finished stage puts the body first', () => {
  const output = [
    'llm-1 · task_parse · 请求中 · m',
    'llm-1 · task_parse · iter 1 · 成功 · m',
    '任务描述：装好依赖',
    'llm-2 · diagnose · 成功 · m',
    '消息产物 main:0：先看 README',
  ].join('\n');
  assert.deepEqual(stageOutputBlocks(output, true), [
    {
      kind: 'requests',
      lines: ['llm-1 · task_parse · 请求中 · m', 'llm-1 · task_parse · iter 1 · 成功 · m'],
      count: 1,
    },
    { kind: 'body', text: '任务描述：装好依赖' },
    { kind: 'requests', lines: ['llm-2 · diagnose · 成功 · m'], count: 1 },
    { kind: 'body', text: '消息产物 main:0：先看 README' },
  ]);
  assert.deepEqual(
    stageOutputBlocks('llm-1 · task_parse · 请求中 · m\n\nllm-1 · task_parse · 成功 · m', true),
    [{
      kind: 'requests',
      lines: ['llm-1 · task_parse · 请求中 · m', 'llm-1 · task_parse · 成功 · m'],
      count: 1,
    }],
  );
  assert.deepEqual(stageOutputBlocks(output, false), [
    { kind: 'body', text: '任务描述：装好依赖\n消息产物 main:0：先看 README' },
    {
      kind: 'requests',
      lines: [
        'llm-1 · task_parse · 请求中 · m',
        'llm-1 · task_parse · iter 1 · 成功 · m',
        'llm-2 · diagnose · 成功 · m',
      ],
      count: 2,
    },
  ]);
  assert.deepEqual(stageOutputBlocks('', true), []);
  assert.deepEqual(stageOutputBlocks('llm-1 · task_parse · 请求中 · m', true), [
    { kind: 'requests', lines: ['llm-1 · task_parse · 请求中 · m'], count: 1 },
  ]);
});

test('processSummary counts stages and adds their durations', () => {
  assert.deepEqual(processSummary([]), { count: 0, elapsed: '' });
  assert.deepEqual(
    processSummary([
      { ...stage('task_parse', 'completed'), elapsed_ms: 1500 },
      { ...stage('diagnose', 'completed'), elapsed_ms: 2500 },
      stage('attribute', 'running'),
    ]),
    { count: 3, elapsed: '4.0s' },
  );
});

test('rewardOverview lists every category with its dimensions and counts the perfect ones', () => {
  const overview = rewardOverview({
    schema_version: 'v1',
    overall: { comment: '  基本完成 ', total_score: 87 },
    categories: [{ name: 'A', score: 93, comment: ' 不错 ' }, { name: 'B' }, { name: 'C', score: 70 }],
    dimensions: [
      { category: 'A', requirement: ' r1 ', comment: ' c1 ', score: 93 },
      { category: 'D', requirement: 'r2', comment: '', score: 80 },
      { category: 'D', requirement: 'r3', comment: '', score: 90 },
    ],
  });
  assert.equal(overview.totalScore, 87);
  assert.equal(overview.comment, '基本完成');
  // B has neither a score nor a dimension and says nothing; D only owns dimensions.
  assert.deepEqual(overview.categories, [
    {
      name: 'A',
      score: 93,
      tier: 'perfect',
      comment: '不错',
      dimensions: [{ requirement: 'r1', comment: 'c1', score: 93, tier: 'perfect' }],
    },
    { name: 'C', score: 70, tier: 'weak', comment: '', dimensions: [] },
    {
      name: 'D',
      score: null,
      tier: null,
      comment: '',
      dimensions: [
        { requirement: 'r2', comment: '', score: 80, tier: 'fair' },
        { requirement: 'r3', comment: '', score: 90, tier: 'perfect' },
      ],
    },
  ]);
  assert.equal(overview.dimensionCount, 3);
  assert.equal(overview.perfectCount, 2);
});