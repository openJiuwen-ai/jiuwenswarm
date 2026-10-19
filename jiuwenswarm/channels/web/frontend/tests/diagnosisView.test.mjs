import assert from 'node:assert/strict';
import test from 'node:test';

import {
  diagnosisTarget,
  filterRecords,
  formatRecordTime,
  formatTimeWindow,
  isStageExpanded,
  recordIconKind,
  recordLabel,
  stageViews,
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
