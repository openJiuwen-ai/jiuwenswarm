import assert from 'node:assert/strict';
import test from 'node:test';
import React from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import i18next from 'i18next';
import { createRequire } from 'node:module';

const require = createRequire(import.meta.url);
require.extensions['.css'] = () => {};
const { I18nextProvider } = require('react-i18next');
const { DiagnosisSummaryTree } = require('../node_modules/.cache/trajectory-diagnosis/DiagnosisSummaryTree.cjs');

const i18n = i18next.createInstance();
await i18n.init({ lng: 'en', resources: { en: { translation: {} } }, initImmediate: false });

function render(record) {
  return renderToStaticMarkup(React.createElement(I18nextProvider, { i18n },
    React.createElement(DiagnosisSummaryTree, { record, subjectLabels: new Map() })));
}

function detailsTag(html, testId) {
  const match = html.match(new RegExp(`<details\\b[^>]*data-testid="${testId}"[^>]*>`));
  assert.ok(match, testId);
  return match[0];
}

test('overall account keeps its text without a section title, and agent accounts stay collapsed', () => {
  const tree = { display_summary: 'Readable outcome for the user', trajectory_summary: 'TECHNICAL_ROOT_ACCOUNT',
    status: 'completed', warnings: [], problems: [], turns: {},
    agent_summaries: { A: { display_name: 'Worker A', summary: 'Detailed agent account', status: 'completed' } } };
  const html = render({ summary_tree: tree });
  assert.ok(html.includes('Readable outcome for the user'));
  assert.ok(html.includes('Detailed agent account'));
  assert.ok(!html.includes('TECHNICAL_ROOT_ACCOUNT'));
  assert.ok(!html.includes('summaryTree.root'));
  assert.equal(tree.trajectory_summary, 'TECHNICAL_ROOT_ACCOUNT');
  assert.ok(html.includes('summaryTree.download'));
  assert.ok(html.includes('summaryTree.agentsHint'));
  assert.ok(!/\bopen\b/.test(detailsTag(html, 'trajectory-diagnosis-summary-agents')));
  assert.ok(!/\bopen\b/.test(detailsTag(html, 'trajectory-diagnosis-summary-agent')));
  assert.ok(html.includes('lucide-circle-check'));
  assert.ok(!html.includes('data-kind="completed"'));
});

test('failed summary keeps its failure mark instead of a success check', () => {
  const html = render({ summary_tree: { trajectory_summary: '', status: 'failed', warnings: [],
    problems: [], turns: {}, agent_summaries: {} } });
  assert.ok(html.includes('data-kind="failed"'));
  assert.ok(!html.includes('lucide-circle-check'));
});

test('summary detail renders the overall account and agent accounts while keeping turn details off the page', () => {
  const root = 'Root context '.repeat(100) + 'ROOT_END_MARKER';
  const segment = { segment_id: 'A:segment:1', start_node_id: 'A:1', end_node_id: 'A:4',
    narrative: 'Distinct segment result', status: 'completed', problems: [] };
  const html = render({ diagnosis_id: 'dg_1', session_id: 's1', input: { scope: { kind: 'full' } },
    summary_tree: { trajectory_summary: root, status: 'degraded', problems: [], warnings: [],
      agent_summaries: { A: { display_name: 'Worker A', summary: 'Distinct agent result', status: 'degraded' } },
      turns: { A: [{ turn_id: 't1', turn_number: 1, narrative: 'Distinct turn result',
        status: 'completed', segments: [segment] }] } } });
  assert.ok(html.includes('ROOT_END_MARKER'));
  assert.ok(!html.includes('summaryTree.root'));
  const order = ['ROOT_END_MARKER', 'Distinct agent result'];
  assert.deepEqual(order.map(text => html.indexOf(text)), [...order.map(text => html.indexOf(text))].sort((a, b) => a - b));
  assert.ok(html.includes('Distinct agent result'));
  assert.ok(!html.includes('Distinct turn result') && !html.includes('Distinct segment result'));
  assert.ok(!html.includes('A:1') && !html.includes('A:4'));
  assert.ok(html.includes('role="region"') && html.includes('tabindex="0"'));
  assert.ok(html.includes('summaryTree.download'));
  assert.ok(!/\bopen\b/.test(detailsTag(html, 'trajectory-diagnosis-summary-agents')));
  assert.ok(!/\bopen\b/.test(detailsTag(html, 'trajectory-diagnosis-summary-agent')));
});

test('legacy fallback hashes are hidden and root does not duplicate every agent account', () => {
  const hash = '489ebd49201b4753bbc98c9bb6d25d9f';
  const html = render({ summary_tree: { trajectory_summary: '归并未完成\nROOT_DUPLICATE_MARKER', status: 'degraded',
    warnings: [{ code: 'merge_failed', message: 'technical details' }], turns: {}, problems: [],
    agent_summaries: { A: { display_name: 'Leader', summary: `[${hash}]\nImportant action retained`, status: 'degraded' } } } });
  assert.ok(!html.includes(hash));
  assert.ok(!html.includes('ROOT_DUPLICATE_MARKER'));
  assert.ok(!html.includes('summaryTree.root'));
  assert.ok(html.includes('Important action retained'));
  assert.ok(html.includes('summaryTree.unmerged'));
  assert.ok(html.includes('data-kind="degraded"'));
});

test('legacy records still display their full root without a hierarchy', () => {
  const text = 'legacy '.repeat(100) + 'LEGACY_END_MARKER';
  const html = render({ trajectory_summary: text });
  assert.ok(html.includes('LEGACY_END_MARKER'));
  assert.ok(!html.includes('summaryTree.root'));
  assert.ok(!html.includes('trajectory-diagnosis-summary-agents'));
});

test('an unmerged agent does not leak nested turn accounts into the summary cards', () => {
  const html = render({ summary_tree: { trajectory_summary: 'Overall account', status: 'degraded', turns: {},
    problems: [], warnings: [], agent_summaries: { A: { display_name: 'Leader', status: 'degraded',
      summary: '归并未完成\nTurn 1:\nNESTED_TURN_ACCOUNT' } } } });
  assert.ok(html.includes('Overall account'));
  assert.ok(!html.includes('summaryTree.root'));
  assert.ok(html.includes('summaryTree.agentUnmerged'));
  assert.ok(!html.includes('NESTED_TURN_ACCOUNT'));
  assert.ok(!/\bopen\b/.test(detailsTag(html, 'trajectory-diagnosis-summary-agents')));
});
