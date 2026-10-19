import assert from 'node:assert/strict';
import test from 'node:test';

import {
  attributionCauses,
  causeNodeLabel,
  diagnosisTarget,
  filterRecords,
  formatElapsed,
  formatRecordTime,
  formatTimeWindow,
  groupCausesBySignal,
  isStageExpanded,
  looksLikePath,
  processDefaultOpen,
  processSummary,
  recordIconKind,
  recordLabel,
  rewardDimensionGroups,
  rewardOverview,
  rewardTier,
  showCategoryBadge,
  signalTier,
  splitLeadParagraph,
  splitRewardSignalText,
  splitStageOutput,
  stageViews,
  suggestionsState,
  ttseTier,
  unixNanoToMs,
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

test('completed stages fold away while the rest stay open', () => {
  assert.equal(isStageExpanded('completed', undefined), false);
  assert.equal(isStageExpanded('running', undefined), true);
  assert.equal(isStageExpanded('failed', undefined), true);
  assert.equal(isStageExpanded('skipped', undefined), true);
});

test('a waiting stage never opens and a user choice beats the default', () => {
  assert.equal(isStageExpanded('pending', undefined), false);
  assert.equal(isStageExpanded('pending', true), false);
  assert.equal(isStageExpanded('completed', true), true);
  assert.equal(isStageExpanded('running', false), false);
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

test('a record row falls back to the scope label without a headline', () => {
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

test('a full-scope record has no window to show', () => {
  assert.deepEqual(diagnosisTarget({ input: { scope: { kind: 'full' } } }), { full: true, groups: [] });
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

test('the category badge hides when it says nothing', () => {
  assert.equal(showCategoryBadge({ category: 'other' }), false);
  assert.equal(showCategoryBadge({ category: 'other', confidence: null }), false);
  assert.equal(showCategoryBadge({ category: 'other', confidence: 0.4 }), true);
  assert.equal(showCategoryBadge({ category: 'skill_defect' }), true);
  assert.equal(showCategoryBadge({ category: 'ttse_fail' }), false);
});

test('attribution causes leave out the experience verdict', () => {
  const causes = attributionCauses({
    root_causes: [cause('a'), cause('b', { category: 'ttse_partial' })],
  });
  assert.deepEqual(causes.map(item => item.id), ['a']);
});

test('causes group by signal id and keep first-seen order', () => {
  const groups = groupCausesBySignal([
    cause('a', { signal: { id: 's1', source: 'reward' }, evidence: 'e1' }),
    cause('b', { signal: { id: 's2', source: 'keyword_tool' }, evidence: 'e2' }),
    cause('c', { signal: { id: 's1', source: 'reward' }, evidence: 'e1' }),
  ]);
  assert.deepEqual(groups.map(group => [group.key, group.causes.map(item => item.id)]), [
    ['signal:s1', ['a', 'c']],
    ['signal:s2', ['b']],
  ]);
  assert.equal(groups[0].evidence, 'e1');
});

test('records without signal references group by identical evidence', () => {
  const groups = groupCausesBySignal([
    cause('a', { evidence: 'same' }),
    cause('b', { evidence: 'same' }),
    cause('c', { evidence: 'other' }),
    cause('d', { evidence: '' }),
    cause('e', { evidence: '' }),
  ]);
  assert.deepEqual(groups.map(group => group.causes.map(item => item.id)), [['a', 'b'], ['c'], ['d'], ['e']]);
  assert.equal(groups[0].signal, null);
});

test('the node label joins distinct node ids with an arrow', () => {
  const ref = nodeId => ({ trace_id: 't', span_id: 's', subject_id: 'main', ...(nodeId === undefined ? {} : { node_id: nodeId }) });
  assert.equal(causeNodeLabel({ refs: [ref('main:23'), ref('main:30')] }), 'main:23 → main:30');
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

test('Reward dimensions group by category with their tiers', () => {
  const groups = rewardDimensionGroups({
    dimensions: [
      { category: '正确性', requirement: 'r1', comment: 'c1', score: 95 },
      { category: '完整性', requirement: 'r2', comment: 'c2', score: 60 },
      { category: '正确性', requirement: 'r3', comment: 'c3', score: 80 },
    ],
  });
  assert.deepEqual(
    groups.map(group => [group.category, group.dimensions.map(item => [item.requirement, item.tier])]),
    [
      ['正确性', [['r1', 'perfect'], ['r3', 'fair']]],
      ['完整性', [['r2', 'weak']]],
    ],
  );
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

test('ttseTier reads success as met, partial as fair and failure as missed', () => {
  assert.equal(ttseTier('ttse_success'), 'perfect');
  assert.equal(ttseTier('ttse_partial'), 'fair');
  assert.equal(ttseTier('ttse_fail'), 'weak');
});

test('the process starts open unless a finished diagnosis has results to read', () => {
  const reward = { schema_version: 'v1', overall: { comment: 'ok', total_score: 90 }, categories: [], dimensions: [] };
  const cause = { id: 'a', category: 'other', summary: 's', refs: [] };
  assert.equal(processDefaultOpen({ status: 'running', root_causes: [], reward: null }), true);
  assert.equal(processDefaultOpen({ status: 'failed', root_causes: [cause], reward }), true);
  assert.equal(processDefaultOpen({ status: 'cancelled', root_causes: [], reward: null }), true);
  assert.equal(processDefaultOpen({ status: 'completed', root_causes: [], reward: null }), true);
  assert.equal(processDefaultOpen({ status: 'completed', root_causes: [cause], reward: null }), false);
  assert.equal(processDefaultOpen({ status: 'completed', root_causes: [], reward }), false);
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

test('rewardOverview keeps scored categories and counts what the details hold', () => {
  const overview = rewardOverview({
    schema_version: 'v1',
    overall: { comment: '  基本完成 ', total_score: 87 },
    categories: [{ name: 'A', score: 93 }, { name: 'B' }, { name: 'C', score: 70 }],
    dimensions: [
      { category: 'A', requirement: 'r1', comment: '', score: 93 },
      { category: 'D', requirement: 'r2', comment: '', score: 80 },
    ],
  });
  assert.equal(overview.totalScore, 87);
  assert.equal(overview.comment, '基本完成');
  assert.deepEqual(overview.categories, [
    { name: 'A', score: 93, tier: 'perfect' },
    { name: 'C', score: 70, tier: 'weak' },
  ]);
  assert.equal(overview.categoryCount, 4);
  assert.equal(overview.dimensionCount, 2);
});