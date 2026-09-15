import assert from 'node:assert/strict';
import { execFileSync } from 'node:child_process';
import { mkdirSync, readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import test from 'node:test';
import { fileURLToPath, pathToFileURL } from 'node:url';

const frontendRoot = fileURLToPath(
  new URL('../../jiuwenswarm/channels/web/frontend/', import.meta.url),
);
const cacheRoot = join(frontendRoot, 'node_modules/.cache/permission-card-presentation');

function buildBundle(entry, outputName, { externalReact = false } = {}) {
  const outputPath = join(cacheRoot, outputName);
  mkdirSync(dirname(outputPath), { recursive: true });
  const args = [
    entry,
    '--bundle',
    '--platform=node',
    '--format=esm',
    `--outfile=${outputPath}`,
    '--define:import.meta.env.DEV=false',
  ];
  if (externalReact) {
    args.push('--packages=external');
  }
  execFileSync(join(frontendRoot, 'node_modules/.bin/esbuild'), args, {
    cwd: frontendRoot,
    shell: false,
    stdio: 'pipe',
  });
  return outputPath;
}

const pendingQueueBundle = buildBundle(
  'src/stores/pendingQuestionQueue.ts',
  'pendingQuestionQueue.mjs',
);
const reviewerBundle = buildBundle(
  'src/components/ChatPanel/AutoReviewerStatus.tsx',
  'AutoReviewerStatus.mjs',
  { externalReact: true },
);
const reviewerMetadataBundle = buildBundle(
  'src/features/tool-events/reviewerMetadata.ts',
  'reviewerMetadata.mjs',
);
const promptBundle = buildBundle(
  'src/components/InteractionSlot/AuthorizationPrompt.tsx',
  'AuthorizationPrompt.mjs',
  { externalReact: true },
);
const { bindPendingPermissionCard } = await import(
  pathToFileURL(pendingQueueBundle).href
);
const {
  reviewerBadgeTone,
  reviewerDecisionSourceCategory,
  reviewerDetailValues,
  reviewerDisplayStatus,
  reviewerNeedsManualAction,
  reviewerRiskLevel,
} =
  await import(pathToFileURL(reviewerBundle).href);
const { normalizeReviewerMetadata } = await import(
  pathToFileURL(reviewerMetadataBundle).href
);
const {
  AuthorizationQuestionDetails,
  AuthorizationPrompt,
  buildAuthorizationAnswers,
  resolveAuthorizationActions,
  formatPermissionPayload,
} = await import(pathToFileURL(promptBundle).href);

function question(cardId) {
  return {
    question: 'Approve?',
    header: 'Permission',
    options: [{ label: 'Approve', value: 'approve' }],
    ...(cardId !== undefined ? { card_id: cardId } : {}),
  };
}

test('binds the backend card id and ignores a caller-provided card id', () => {
  const answers = [{ selected_options: ['approve'], card_id: 'caller-controlled' }];
  const result = bindPendingPermissionCard(answers, [question('  invocation-1  ')]);

  assert.deepEqual(result, [
    {
      selected_options: ['approve'],
      card_id: 'invocation-1',
    },
  ]);
  assert.equal(answers[0].card_id, 'caller-controlled');
});

test('preserves legacy answers and rejects invalid smart card bindings', () => {
  const answer = [{ selected_options: ['approve'] }];
  assert.deepEqual(bindPendingPermissionCard(answer, [question(undefined)]), answer);
  assert.deepEqual(bindPendingPermissionCard(answer, [question('x'.repeat(129))]), []);
  assert.deepEqual(
    bindPendingPermissionCard(
      [answer[0], answer[0]],
      [question('invocation-1'), question('invocation-2')],
    ),
    [],
  );
});

test('missing card semantics fail closed instead of selecting the first allow option', () => {
  const picked = {
    semantic: 'allow-always',
    option: { label: 'Always', value: 'allow_always' },
    label: 'Always',
    tip: '',
  };
  const questions = [
    {
      ...question('invocation-1'),
      options: [
        { label: 'Once', value: 'allow_once' },
        { label: 'Reject', value: 'reject' },
      ],
    },
  ];

  assert.deepEqual(buildAuthorizationAnswers(questions, picked, true), [
    { selected_options: ['reject'] },
  ]);
  assert.deepEqual(buildAuthorizationAnswers(questions, picked), [
    { selected_options: ['allow_once'] },
  ]);
});

test('resolves trusted badge tones with failure states taking precedence', () => {
  assert.equal(
    reviewerBadgeTone({ final_reviewer_status: 'approved', decision_source: 'auto_reviewer' }),
    'success',
  );
  assert.equal(
    reviewerBadgeTone({ final_reviewer_status: 'approved', decision_source: 'manual_approval' }),
    'info',
  );
  assert.equal(
    reviewerBadgeTone({ final_reviewer_status: 'denied', decision_source: 'manual_approval' }),
    'danger',
  );
  assert.equal(
    reviewerBadgeTone({ final_reviewer_status: 'manual', decision_source: 'auto_reviewer' }),
    'warning',
  );
  assert.equal(
    reviewerBadgeTone({ final_reviewer_status: 'host_revalidation_failed' }),
    'danger',
  );
  assert.equal(reviewerBadgeTone({ final_reviewer_status: 'aborted' }), 'neutral');
});

test('keeps legacy multi-question actions and per-question fallback', () => {
  const questions = [question(), { ...question(), options: [{ label: 'Continue', value: 'continue' }] }];
  const actions = resolveAuthorizationActions(questions);
  assert.equal(actions.length, 1);
  assert.deepEqual(buildAuthorizationAnswers(questions, actions[0]), [
    { selected_options: ['approve'] }, { selected_options: ['continue'] },
  ]);
  assert.equal(normalizeReviewerMetadata({ metadata: { risk_level: 'high', decision_source: 'tool' } }), undefined);
});

test('classifies reviewer decision sources without exposing internal values', () => {
  assert.equal(reviewerDecisionSourceCategory('auto_reviewer'), 'automatic');
  assert.equal(reviewerDecisionSourceCategory('manual_approval'), 'manual');
  assert.equal(reviewerDecisionSourceCategory('final_gate'), 'system');
  assert.equal(reviewerDecisionSourceCategory('unknown_future_gate'), 'system');
  assert.equal(reviewerDecisionSourceCategory(''), undefined);
});

test('never upgrades intermediate approval into a terminal badge', () => {
  for (const decision_source of ['auto_reviewer', 'manual_approval']) {
    const reviewer = { reviewer_status: 'approved', decision_source };
    assert.equal(reviewerDisplayStatus(reviewer), undefined);
    assert.equal(reviewerBadgeTone(reviewer), 'neutral');
  }
  assert.equal(
    reviewerDisplayStatus({
      final_reviewer_status: 'approved',
      reviewer_status: 'denied',
      decision_source: 'manual_approval',
    }),
    'denied',
  );
  assert.equal(
    reviewerBadgeTone({
      final_reviewer_status: 'approved',
      reviewer_status: 'denied',
      decision_source: 'manual_approval',
    }),
    'danger',
  );
});

test('normalizes a single reviewer reason without audit-only fields', () => {
  assert.deepEqual(
    reviewerDetailValues({
      manual_reason_summary: '  inspect this risk  ',
      evidence_summary: 'inspect this risk',
      final_reviewer_status: 'manual',
    }),
    { reason: 'inspect this risk' },
  );
  assert.equal(
    reviewerDetailValues({ evidence_summary: 'evidence reason' }).reason,
    'evidence reason',
  );
  assert.equal(reviewerDetailValues({}).reason, undefined);
  assert.equal(reviewerRiskLevel({ risk_level: 'HIGH' }), 'high');
  assert.equal(reviewerRiskLevel({ risk_level: 'unexpected' }), 'unknown');
  assert.equal(reviewerNeedsManualAction({ final_reviewer_status: 'manual' }), true);
  assert.equal(reviewerNeedsManualAction({ final_reviewer_status: 'approved' }), false);
  assert.equal(
    normalizeReviewerMetadata({ metadata: { action_summary: 'audit only' } }),
    undefined,
  );
});

test('formats display-only payload copies without changing their structure', () => {
  assert.equal(formatPermissionPayload({ command: 'echo safe' }), '{\n  "command": "echo safe"\n}');
  assert.equal(formatPermissionPayload('[UNAVAILABLE]'), '"[UNAVAILABLE]"');
});

test('renders the current permission card reviewer and redacted payload', async () => {
  const React = (
    await import(pathToFileURL(join(frontendRoot, 'node_modules/react/index.js')).href)
  ).default;
  const { renderToStaticMarkup } = await import(
    pathToFileURL(join(frontendRoot, 'node_modules/react-dom/server.node.js')).href
  );
  const { createInstance } = await import(
    pathToFileURL(join(frontendRoot, 'node_modules/i18next/dist/esm/i18next.js')).href
  );
  const { I18nextProvider } = await import(
    pathToFileURL(join(frontendRoot, 'node_modules/react-i18next/dist/es/index.js')).href
  );
  const resources = JSON.parse(
    readFileSync(join(frontendRoot, 'src/i18n/locales/en.json'), 'utf8'),
  );
  const i18n = createInstance();
  await i18n.init({ lng: 'en', resources: { en: { translation: resources } } });
  const questions = [
    {
      question: 'Approve first?',
      header: 'Permission: first_tool',
      options: [],
      card_id: 'card-1',
      tool_payload: { command: 'first-payload' },
      reviewer_metadata: {
        final_reviewer_status: 'approved',
        decision_source: 'auto_reviewer',
        risk_level: 'low',
        evidence_summary: 'first-reason',
        manual_reason_summary: 'stale-manual-reason',
        user_review_hint: 'stale-manual-hint',
      },
    },
  ];
  const html = renderToStaticMarkup(
    React.createElement(
      I18nextProvider,
      { i18n },
      React.createElement(AuthorizationQuestionDetails, {
        questions,
        requestId: 'request-1',
      }),
    ),
  );

  for (const expected of [
    'authorization-question-0',
    'permission-tool-payload-0',
    'first-payload',
    'first-reason',
    'data-badge-tone="success"',
    'Tool payload \\(redacted\\)',
    'Risk: </span><span class="break-words text-text">Low',
  ]) {
    assert.match(html, new RegExp(expected));
  }
  assert.doesNotMatch(html, /stale-manual-reason/);
  assert.doesNotMatch(html, /stale-manual-hint/);
  assert.doesNotMatch(html, /Authorization/);
  assert.doesNotMatch(html, /Missing evidence/);
  for (const source of ['permission_interrupt', 'confirm_interrupt', 'activate_confirm', 'evolution_interrupt']) {
    for (const withCard of [false, true]) {
      const pending = { request_id: 'request-1', source, questions: [question(withCard ? 'card-1' : undefined)] };
      const prompt = renderToStaticMarkup(React.createElement(I18nextProvider, { i18n },
        React.createElement(AuthorizationPrompt, { pending, onSubmit: async () => true })));
      assert.equal(prompt.includes('auth-prompt--smart'), withCard && source === 'permission_interrupt');
    }
  }
});

test('keeps required permission labels in both language resources', () => {
  const en = JSON.parse(readFileSync(join(frontendRoot, 'src/i18n/locales/en.json'), 'utf8'));
  const zh = JSON.parse(readFileSync(join(frontendRoot, 'src/i18n/locales/zh.json'), 'utf8'));
  for (const resource of [en, zh]) {
    assert.ok(resource.authPrompt.toolPayload.title);
    assert.ok(resource.authPrompt.toolPayload.notice);
    assert.ok(resource.chatUi.autoReviewer.details.riskValues.high);
    assert.ok(resource.chatUi.autoReviewer.details.riskValues.unknown);
    assert.ok(resource.chatUi.autoReviewer.details.reasonValues.manual);
    assert.ok(resource.chatUi.autoReviewer.details.manualGuidanceFallback);
  }
});
