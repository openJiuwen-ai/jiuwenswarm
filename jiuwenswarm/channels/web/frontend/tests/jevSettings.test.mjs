import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

const bundle = await build({
  entryPoints: ['src/features/settings/modules/browser/jevSettings.ts'],
  bundle: true, format: 'esm', platform: 'node', write: false,
});
const { JEV_PRESETS, createEmptyJevDraft, createJevDraft, validateJevDraft } = await import(
  `data:text/javascript;base64,${Buffer.from(bundle.outputFiles[0].text).toString('base64')}`
);

test('new Jev drafts require an explicit provider selection', () => {
  const draft = createEmptyJevDraft();
  assert.deepEqual(draft, { provider: '', api_base: '', api_key: '', model: '', display_name: '' });
  assert.ok(validateJevDraft(draft).provider);
});

test('Jev offers only OpenRouter, TypeSafe and custom templates', () => {
  assert.deepEqual(Object.keys(JEV_PRESETS), ['openrouter', 'typesafe', 'custom']);
  for (const provider of Object.keys(JEV_PRESETS)) {
    const draft = createJevDraft({ decision_provider: provider }, {
      jev_openrouter_api_key: 'router-key', jev_typesafe_api_key: 'typesafe-key', jev_custom_api_key: 'custom-key',
    });
    assert.equal(draft.provider, provider);
    assert.equal(draft.api_base, JEV_PRESETS[provider].api_base);
    assert.equal(draft.model, JEV_PRESETS[provider].model);
    assert.equal(draft.display_name, JEV_PRESETS[provider].model);
    assert.equal(draft.api_key, provider === 'openrouter' ? 'router-key' : `${provider}-key`);
  }
});

test('saved model and base are restored without replacing the saved credential', () => {
  const draft = createJevDraft({
    decision_provider: 'custom', decision_model: 'jev-preview', decision_api_base: 'https://jev.example.test/v1',
    decision_display_name: 'My Jev',
  }, { jev_custom_api_key: 'synthetic-key' });
  assert.deepEqual(draft, {
    provider: 'custom', model: 'jev-preview', api_base: 'https://jev.example.test/v1', api_key: 'synthetic-key',
    display_name: 'My Jev',
  });
  assert.deepEqual(validateJevDraft(draft), {});
});

test('custom requires an HTTPS base, a model and a key', () => {
  const draft = createJevDraft({ decision_provider: 'custom' }, {});
  assert.ok(validateJevDraft(draft).api_base);
  assert.ok(validateJevDraft(draft).api_key);
  assert.ok(validateJevDraft({ ...draft, model: ' ' }).model);
  for (const api_base of ['http://example.test', 'https://u:p@example.test', 'https://example.test?k=secret', 'https://example.test/#x']) {
    assert.ok(validateJevDraft({ ...draft, api_base }).api_base);
  }
});

test('OpenRouter model names are Jev-specific, not chat model names', () => {
  const draft = createJevDraft({ decision_provider: 'openrouter' }, { jev_openrouter_api_key: 'synthetic-key' });
  assert.deepEqual(validateJevDraft(draft), {});
  assert.deepEqual(validateJevDraft({ ...draft, model: '~typesafe/jev-latest' }), {});
  assert.ok(validateJevDraft({ ...draft, model: 'ordinary-chat-model' }).model);
});

test('display names are required, bounded, and independent from model names', () => {
  const draft = createJevDraft({ decision_provider: 'typesafe' }, { jev_typesafe_api_key: 'synthetic-key' });
  assert.ok(validateJevDraft({ ...draft, display_name: ' ' }).display_name);
  assert.ok(validateJevDraft({ ...draft, display_name: 'x'.repeat(101) }).display_name);
  assert.deepEqual(validateJevDraft({ ...draft, display_name: '决策模型'.repeat(25) }), {});
});
