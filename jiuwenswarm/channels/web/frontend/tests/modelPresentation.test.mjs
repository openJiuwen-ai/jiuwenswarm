import assert from 'node:assert/strict';
import test from 'node:test';
import { build } from 'esbuild';

await build({
  entryPoints: [
    'src/features/settings/modules/models/modelVendorLabels.ts',
    'src/features/settings/modules/models/modelPresentation.ts',
    'src/features/settings/modules/models/modelTranslation.ts',
  ],
  outdir: 'node_modules/.cache/model-presentation',
  bundle: true,
  platform: 'node',
  format: 'esm',
  packages: 'external',
  outExtension: { '.js': '.mjs' },
});

const { getPresetLabel, getVendorLabel } = await import(
  '../node_modules/.cache/model-presentation/modelVendorLabels.mjs'
);
const { formatModelMetadata, getModelDisplayName } = await import(
  '../node_modules/.cache/model-presentation/modelPresentation.mjs'
);

const plainT = (key, options = {}) => {
  if (key === 'settingsPanel.models.vendors.zhipu') return '智谱';
  if (key === 'settingsPanel.models.vendors.unrecognized') return `未知厂商(${options.vendor})`;
  if (key === 'settingsPanel.models.customVendor') return '自定义';
  if (key === 'settingsPanel.models.openaiAccount') return 'OpenAI 账号';
  if (key === 'settingsPanel.models.protocols.openai') return 'OpenAI';
  if (key === 'settingsPanel.models.protocols.anthropic') return 'Anthropic';
  return key;
};

/** Mimics i18next TFunction: branded callable that still satisfies ModelTranslate. */
function brandedT(key, options) {
  return plainT(key, options);
}
brandedT.$TFunctionBrand = 'translation';

test('known vendors translate through a plain translator', () => {
  assert.equal(getVendorLabel('zhipu', plainT), '智谱');
  assert.equal(getPresetLabel({ vendor_key: 'zhipu' }, plainT), '智谱');
});

test('unrecognized vendors keep the vendor key interpolation', () => {
  assert.equal(getVendorLabel('acme-corp', plainT), '未知厂商(acme-corp)');
});

test('display name prefers alias and falls back to model_name', () => {
  assert.equal(getModelDisplayName({ model_name: 'GLM-5.2', alias: '主模型' }), '主模型');
  assert.equal(getModelDisplayName({ model_name: 'GLM-5.2', alias: '  ' }), 'GLM-5.2');
});

test('metadata keeps custom vendor, OpenAI account, and known vendor branches', () => {
  assert.equal(
    formatModelMetadata(
      { model_name: 'GLM-5.2', model_provider: 'OpenAI', vendor_key: 'zhipu' },
      plainT,
    ),
    '智谱 · OpenAI · GLM-5.2',
  );
  assert.equal(
    formatModelMetadata(
      { model_name: 'custom-model', model_provider: 'OpenAI', vendor_key: '' },
      plainT,
    ),
    '自定义 · OpenAI · custom-model',
  );
  assert.equal(
    formatModelMetadata({ model_name: 'gpt-4o', model_provider: 'OpenAIAccount' }, plainT),
    'OpenAI 账号 · OpenAI · gpt-4o',
  );
});

test('plain translators and branded useTranslation-like callables are both accepted', () => {
  assert.equal(getVendorLabel('zhipu', brandedT), '智谱');
  assert.equal(
    formatModelMetadata(
      { model_name: 'GLM-5.2', model_provider: 'OpenAI', vendor_key: 'zhipu' },
      brandedT,
    ),
    '智谱 · OpenAI · GLM-5.2',
  );
});
