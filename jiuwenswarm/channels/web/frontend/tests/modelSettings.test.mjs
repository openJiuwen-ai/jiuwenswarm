import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

import {
  CUSTOM_VENDOR_SELECTION,
  OPENAI_ACCOUNT_DEFAULT_API_BASE,
  OPENAI_ACCOUNT_SELECTION,
  applyModelProtocol,
  applyVendorSelection,
  createModelDraft,
  modelDraftToEntry,
  normalizeModelOptions,
  parseVendorCatalog,
  rebaseModelDraft,
  reconcileModelReasoning,
  resolveModelPreset,
  selectProviderDefaultModel,
  syncDisplayNameWithModelName,
  vendorSelectionKey,
} from '../node_modules/.cache/settings-refactor/modules/models/modelAdapters.js';
import { validateModelDraft } from '../node_modules/.cache/settings-refactor/modules/models/modelValidation.js';
import {
  LOGIN_MODEL_SOURCE,
  getEditableModels,
  getModelDisplayGroups,
} from '../node_modules/.cache/settings-refactor/modules/models/modelListOperations.js';

const root = new URL('../', import.meta.url);
const source = (path) => readFileSync(new URL(path, root), 'utf8');
const t = (key, values = {}) => `${key}:${JSON.stringify(values)}`;
const reasoning = {
  protocol_defaults: {
    openai: { options: ['off', 'low', 'medium', 'high'], recommended: null },
    anthropic: { options: ['off', 'low', 'medium', 'high', 'max'], recommended: null },
  },
  model_fallbacks: [],
};

function preset(overrides = {}) {
  return {
    vendor_key: 'alibaba',
    display_name: 'backend display name is not UI copy',
    plan: 'token_plan',
    client_provider: 'OpenAI',
    api_base: 'https://openai.example/v1',
    endpoint_profile: 'dashscope',
    default_model: 'qwen-default',
    model_options: ['qwen-default', 'qwen-next'],
    icon_key: 'qwen',
    models_endpoint: 'https://openai.example/v1/models',
    models_needs_key: true,
    supports_anthropic: true,
    anthropic_base: 'https://anthropic.example',
    anthropic_client_provider: 'Anthropic',
    reasoning_capabilities: {},
    reasoning_rules: [],
    ...overrides,
  };
}

const catalog = parseVendorCatalog({
  reasoning,
  token_plan: [preset()],
  coding_plan: [preset({ plan: 'coding_plan', api_base: 'https://coding.example/v1' })],
  custom_api: [preset({ vendor_key: 'openrouter', plan: 'custom_api', endpoint_profile: 'openrouter' })],
});

test('vendor catalog validation is strict and plan identity is never inferred from another field', () => {
  assert.throws(
    () =>
      parseVendorCatalog({
        reasoning,
        token_plan: [preset({ plan: 'coding_plan' })],
        coding_plan: [],
        custom_api: [],
      }),
    /INVALID_VENDOR_CATALOG/,
  );
  assert.equal(resolveModelPreset({ vendor_key: 'alibaba' }, catalog), undefined);
  assert.equal(resolveModelPreset({ vendor_key: 'alibaba', plan: 'coding_plan' }, catalog)?.plan, 'coding_plan');
  assert.equal(resolveModelPreset({ vendor_key: 'openrouter' }, catalog)?.plan, 'custom_api');
});

test('preset protocol mapping uses only the exact server fields', () => {
  const draft = applyVendorSelection(
    createModelDraft(undefined, catalog),
    vendorSelectionKey('token_plan', 'alibaba'),
    catalog,
  );
  assert.equal(draft.model_name, '');
  assert.equal(createModelDraft(undefined, catalog).protocol, 'openai');
  const openAi = modelDraftToEntry({ ...draft, alias: 'qwen-main', api_key: 'secret' }, undefined, catalog, true);
  assert.equal(openAi.model_provider, 'OpenAI');
  assert.equal(openAi.api_base, 'https://openai.example/v1');
  assert.equal(openAi.endpoint_profile, 'dashscope');
  assert.equal(openAi.vendor_key, 'alibaba');
  assert.equal(openAi.plan, 'token_plan');
  assert.equal('config_id' in openAi, false);

  const anthropicDraft = applyModelProtocol(draft, 'anthropic', catalog);
  const anthropic = modelDraftToEntry(
    { ...anthropicDraft, alias: 'qwen-anthropic', api_key: 'secret' },
    undefined,
    catalog,
    true,
  );
  assert.equal(anthropic.model_provider, 'Anthropic');
  assert.equal(anthropic.api_base, 'https://anthropic.example');
  assert.equal('endpoint_profile' in anthropic, false);
});

test('model context windows stay editable and expose common presets', () => {
  const freshDraft = createModelDraft(undefined, catalog);
  assert.equal(freshDraft.context_window_tokens, '256K');
  assert.equal('context_window_1m_enabled' in freshDraft, false);

  const saved = modelDraftToEntry(
    {
      ...freshDraft,
      vendor_selection: CUSTOM_VENDOR_SELECTION,
      model_name: 'custom-model',
      context_window_tokens: '200k',
      api_key: 'secret',
      api_base: 'https://custom.example/v1',
    },
    undefined,
    catalog,
    true,
  );
  assert.equal(saved.context_window_tokens, 200 * 1024);

  const edited = modelDraftToEntry(
    {
      ...freshDraft,
      vendor_selection: CUSTOM_VENDOR_SELECTION,
      model_name: 'custom-model',
      context_window_tokens: '200k',
      api_key: 'secret',
      api_base: 'https://custom.example/v1',
    },
    undefined,
    catalog,
    true,
  );
  assert.equal(edited.context_window_tokens, 200 * 1024);

  const modelDialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  assert.match(modelDialog, /name: 'context_window_tokens'/);
  assert.match(modelDialog, /<ContextWindowField/);
  assert.doesNotMatch(modelDialog, /CONTEXT_WINDOW_1M_FIELD/);
  assert.match(modelDialog, /settingsPanel\.models\.contextWindowHint/);

  const contextWindow = source('src/features/settings/modules/models/contextWindow.ts');
  assert.match(contextWindow, /CONTEXT_WINDOW_PRESETS = \['128K', '256K', '512K', '1M'\]/);
});

test('switching providers clears credentials before applying the next connection preset', () => {
  const startingDraft = {
    ...createModelDraft(undefined, catalog),
    api_key: 'old-provider-key',
  };
  const presetDraft = applyVendorSelection(startingDraft, vendorSelectionKey('token_plan', 'alibaba'), catalog);
  assert.equal(presetDraft.api_key, '');
  assert.equal(applyVendorSelection(startingDraft, CUSTOM_VENDOR_SELECTION, catalog).api_key, '');

  const accountDraft = applyVendorSelection(startingDraft, OPENAI_ACCOUNT_SELECTION, catalog);
  assert.equal(accountDraft.vendor_selection, OPENAI_ACCOUNT_SELECTION);
  assert.equal(accountDraft.protocol, 'openai');
  assert.equal(accountDraft.model_input_mode, 'options');
  assert.equal(accountDraft.model_name, '');
  assert.equal(accountDraft.api_key, '');
  assert.equal(accountDraft.api_base, OPENAI_ACCOUNT_DEFAULT_API_BASE);
});

test('OpenAI account drafts use OAuth credentials while preserving the shared model fields', () => {
  const accountDraft = {
    ...applyVendorSelection(createModelDraft(undefined, catalog), OPENAI_ACCOUNT_SELECTION, catalog),
    alias: 'my-codex',
    model_name: 'gpt-5.2-codex',
    reasoning_level: 'high',
  };
  assert.deepEqual(validateModelDraft(accountDraft, [], undefined, catalog, t), {});
  const entry = modelDraftToEntry(accountDraft, undefined, catalog, true);
  assert.equal(entry.model_provider, 'OpenAIAccount');
  assert.equal(entry.api_base, OPENAI_ACCOUNT_DEFAULT_API_BASE);
  assert.equal(entry.api_key, '');
  assert.equal(entry.model_name, 'gpt-5.2-codex');
  assert.equal(entry.alias, 'my-codex');
  assert.equal(entry.reasoning_level, 'high');
  assert.equal('vendor_key' in entry, false);
  assert.equal('plan' in entry, false);
});

test('catalog initialization rebases system fields without absorbing user edits', () => {
  const model = {
    alias: 'saved-name',
    model_name: 'qwen-default',
    api_key: 'saved-key',
    api_base: 'https://openai.example/v1',
    model_provider: 'OpenAI',
    vendor_key: 'alibaba',
    plan: 'token_plan',
  };
  const emptyCatalog = { token_plan: [], coding_plan: [], custom_api: [] };
  const previousBaseline = createModelDraft(model, emptyCatalog);
  const nextBaseline = createModelDraft(model, catalog);

  assert.deepEqual(rebaseModelDraft(previousBaseline, previousBaseline, nextBaseline), nextBaseline);
  assert.deepEqual(rebaseModelDraft({ ...previousBaseline, alias: 'user edit' }, previousBaseline, nextBaseline), {
    ...nextBaseline,
    alias: 'user edit',
  });
  assert.deepEqual(
    rebaseModelDraft({ ...previousBaseline, alias: 'saved-name' }, previousBaseline, nextBaseline),
    nextBaseline,
  );
});

test('provider model options are normalized and provider changes choose a valid default', () => {
  assert.deepEqual(normalizeModelOptions([' qwen-default ', '', 'qwen-next', 'qwen-default']), [
    'qwen-default',
    'qwen-next',
  ]);
  assert.equal(selectProviderDefaultModel('qwen-next', ['qwen-default', 'qwen-next']), 'qwen-next');
  assert.equal(selectProviderDefaultModel('removed-model', ['qwen-default', 'qwen-next']), 'qwen-default');
  assert.equal(selectProviderDefaultModel('removed-model', []), '');

  const draft = createModelDraft(
    {
      alias: 'existing',
      model_name: 'removed-model',
      api_base: 'https://openai.example/v1',
      api_key: 'secret',
      model_provider: 'OpenAI',
      vendor_key: 'alibaba',
      plan: 'token_plan',
    },
    catalog,
  );
  assert.equal(draft.model_input_mode, 'options');

  const unresolvedLegacyDraft = createModelDraft(
    {
      ...draft,
      model_name: 'legacy-qwen',
      vendor_key: 'alibaba',
      plan: undefined,
    },
    catalog,
  );
  assert.equal(unresolvedLegacyDraft.vendor_selection, '');
  assert.equal(unresolvedLegacyDraft.model_input_mode, 'manual');
});

test('editing a model seeds empty display name from the model name and syncs on model changes', () => {
  const legacyModel = {
    alias: '',
    model_name: 'qwen-existing',
    api_base: 'https://openai.example/v1',
    api_key: '',
    model_provider: 'OpenAI',
    vendor_key: 'alibaba',
    plan: 'token_plan',
  };
  assert.equal(createModelDraft(legacyModel, catalog).alias, 'qwen-existing');
  assert.equal(createModelDraft({ ...legacyModel, alias: 'my-qwen' }, catalog).alias, 'my-qwen');

  assert.equal(
    syncDisplayNameWithModelName(
      {
        alias: '',
        protocol: 'openai',
        vendor_selection: CUSTOM_VENDOR_SELECTION,
        model_name: 'glm-new',
        model_input_mode: 'manual',
        api_key: 'secret',
        api_base: 'https://custom.example/v1',
        reasoning_level: '',
        context_window_tokens: '262144',
        is_default: false,
      },
      '',
    ).alias,
    'glm-new',
  );
  assert.equal(
    syncDisplayNameWithModelName(
      {
        alias: 'qwen-existing',
        protocol: 'openai',
        vendor_selection: CUSTOM_VENDOR_SELECTION,
        model_name: 'glm-new',
        model_input_mode: 'manual',
        api_key: 'secret',
        api_base: 'https://custom.example/v1',
        reasoning_level: '',
        context_window_tokens: '262144',
        is_default: false,
      },
      'qwen-existing',
    ).alias,
    'glm-new',
  );
  assert.equal(
    syncDisplayNameWithModelName(
      {
        alias: 'my-custom',
        protocol: 'openai',
        vendor_selection: CUSTOM_VENDOR_SELECTION,
        model_name: 'glm-new',
        model_input_mode: 'manual',
        api_key: 'secret',
        api_base: 'https://custom.example/v1',
        reasoning_level: '',
        context_window_tokens: '262144',
        is_default: false,
      },
      'qwen-existing',
    ).alias,
    'my-custom',
  );

  const dialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  assert.match(dialog, /syncDisplayNameWithModelName/);
  assert.match(dialog, /name: 'alias'[\s\S]{0,180}required: true/);
});

test('custom models never submit vendor metadata and legacy connection fields remain unchanged until edited', () => {
  const legacy = {
    model_name: 'legacy-model',
    alias: 'legacy',
    api_base: 'https://legacy.example/v1',
    api_key: 'secret',
    model_provider: 'LegacyCompatibleProvider',
    endpoint_profile: 'legacy-profile',
    origin_index: 7,
  };
  const draft = createModelDraft(legacy, catalog);
  assert.equal(draft.vendor_selection, CUSTOM_VENDOR_SELECTION);
  assert.equal(draft.protocol, 'openai');
  const untouched = modelDraftToEntry(draft, legacy, catalog, false);
  assert.equal(untouched.model_provider, 'LegacyCompatibleProvider');
  assert.equal(untouched.endpoint_profile, 'legacy-profile');
  assert.equal(untouched.origin_index, 7);
  assert.equal('vendor_key' in untouched, false);
  assert.equal('plan' in untouched, false);

  const edited = modelDraftToEntry({ ...draft, api_base: 'https://new.example/v1' }, legacy, catalog, true);
  assert.equal(edited.model_provider, 'OpenAI');
  assert.equal('endpoint_profile' in edited, false);
});

test('display name validation is required, unique among display names, and excludes the edited row', () => {
  const models = [
    {
      model_name: 'same-model',
      alias: 'first',
      api_base: 'https://one.example/v1',
      api_key: 'one',
      model_provider: 'OpenAI',
      origin_index: 3,
    },
    {
      model_name: 'other-model',
      alias: 'second',
      api_base: 'https://two.example/v1',
      api_key: 'two',
      model_provider: 'OpenAI',
      origin_index: 9,
    },
  ];
  const baseDraft = {
    alias: '',
    protocol: 'openai',
    vendor_selection: CUSTOM_VENDOR_SELECTION,
    model_name: 'new-model',
    model_input_mode: 'manual',
    api_key: 'secret',
    api_base: 'https://custom.example/v1',
    reasoning_level: '',
    context_window_tokens: '262144',
    is_default: false,
  };
  assert.match(validateModelDraft(baseDraft, models, undefined, catalog, t).alias, /aliasRequired/);
  assert.match(validateModelDraft({ ...baseDraft, alias: 'second' }, models, 3, catalog, t).alias, /aliasConflict/);
  // Matching another model's ID (not its explicit display name) is allowed.
  assert.equal(
    validateModelDraft({ ...baseDraft, alias: 'same-model' }, models, undefined, catalog, t).alias,
    undefined,
  );
  assert.equal(validateModelDraft({ ...baseDraft, alias: 'first' }, models, 3, catalog, t).alias, undefined);
  assert.equal(
    validateModelDraft({ ...baseDraft, alias: 'unique', model_name: 'same-model' }, models, undefined, catalog, t)
      .model_name,
    undefined,
  );
  // Model ID may match another configuration's display name (#5808).
  assert.equal(
    validateModelDraft({ ...baseDraft, alias: 'unique', model_name: 'second' }, models, undefined, catalog, t)
      .model_name,
    undefined,
  );

  // Empty-alias peers still display model_name; that visible name must stay unique.
  const legacyDuplicateModels = [
    {
      model_id: 'mdl_a',
      model_name: 'GLM-5.2',
      alias: '',
      api_base: 'https://one.example/v1',
      api_key: 'one',
      model_provider: 'OpenAI',
    },
    {
      model_id: 'mdl_b',
      model_name: 'GLM-5.2',
      alias: '',
      api_base: 'https://two.example/v1',
      api_key: 'two',
      model_provider: 'OpenAI',
    },
  ];
  assert.match(
    validateModelDraft(
      { ...baseDraft, alias: 'GLM-5.2', model_name: 'GLM-5.2' },
      legacyDuplicateModels,
      { model_id: 'mdl_a' },
      catalog,
      t,
    ).alias,
    /aliasConflict/,
  );
  assert.match(
    validateModelDraft(
      { ...baseDraft, alias: 'GLM-5.2', model_name: 'glm-5.2' },
      legacyDuplicateModels,
      undefined,
      catalog,
      t,
    ).alias,
    /aliasConflict/,
  );
  assert.equal(
    validateModelDraft(
      { ...baseDraft, alias: 'GLM-5.2-custom', model_name: 'GLM-5.2' },
      legacyDuplicateModels,
      { model_id: 'mdl_a' },
      catalog,
      t,
    ).alias,
    undefined,
  );

  // Upsert edit path: entries use model_id without origin_index. Display name equal to own
  // model_name must not be treated as a conflict with self.
  const upsertModels = [
    {
      model_id: 'mdl_self',
      model_name: 'GLM-5.3-Flash',
      alias: 'GLM-5.3-Flash',
      api_base: 'https://custom.example/v1',
      api_key: 'secret',
      model_provider: 'OpenAI',
    },
    {
      model_id: 'mdl_other',
      model_name: 'other-model',
      alias: 'other-alias',
      api_base: 'https://two.example/v1',
      api_key: 'two',
      model_provider: 'OpenAI',
    },
  ];
  const upsertDraft = {
    ...baseDraft,
    alias: 'GLM-5.3-Flash',
    model_name: 'GLM-5.3-Flash',
  };
  assert.match(validateModelDraft(upsertDraft, upsertModels, undefined, catalog, t).alias, /aliasConflict/);
  assert.equal(
    validateModelDraft(upsertDraft, upsertModels, { model_id: 'mdl_self' }, catalog, t).alias,
    undefined,
  );
  assert.match(
    validateModelDraft(
      { ...upsertDraft, alias: 'other-alias' },
      upsertModels,
      { model_id: 'mdl_self' },
      catalog,
      t,
    ).alias,
    /aliasConflict/,
  );

  // Hidden free/login models are not Settings-editable; their model_name must not
  // block a custom display name that happens to match (#5808 review).
  const runtimeGrantedModels = [
    {
      model_name: 'qwen3-max',
      alias: '',
      is_free: true,
      api_base: 'https://free.example/v1',
      api_key: '',
      model_provider: 'OpenAI',
    },
    {
      model_name: 'login-model',
      alias: '',
      source: LOGIN_MODEL_SOURCE,
      api_base: 'https://login.example/v1',
      api_key: '',
      model_provider: 'OpenAI',
    },
  ];
  assert.equal(
    validateModelDraft(
      { ...baseDraft, alias: 'qwen3-max', model_name: 'qwen3-max' },
      runtimeGrantedModels,
      undefined,
      catalog,
      t,
    ).alias,
    undefined,
  );
  assert.equal(
    validateModelDraft(
      { ...baseDraft, alias: 'login-model', model_name: 'custom-id' },
      runtimeGrantedModels,
      undefined,
      catalog,
      t,
    ).alias,
    undefined,
  );
});

test('model group dialog validates unique group names and vendor switch keeps display-name sync', () => {
  const groupDialog = source('src/features/settings/modules/models/ModelGroupDialog.tsx');
  const dialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  assert.match(groupDialog, /modelGroups/);
  assert.match(groupDialog, /modelGroupNameConflict/);
  assert.match(groupDialog, /item === group \|\|[\s\S]{0,120}model_group_id/);
  assert.match(
    dialog,
    /alias === previousModelName \|\| alias === previousSync[\s\S]{0,80}alias: ''/,
  );
  assert.match(dialog, /aliasSyncedFromModelName\.current = ''/);
});

test('model API keys accept 2048 characters and reject longer values', () => {
  const draft = {
    alias: '',
    protocol: 'openai',
    vendor_selection: CUSTOM_VENDOR_SELECTION,
    model_name: 'model',
    model_input_mode: 'manual',
    api_base: 'https://custom.example/v1',
    reasoning_level: '',
    context_window_tokens: '262144',
    is_default: false,
  };
  assert.equal(
    validateModelDraft({ ...draft, api_key: 'k'.repeat(2048) }, [], undefined, catalog, t).api_key,
    undefined,
  );
  assert.match(
    validateModelDraft({ ...draft, api_key: 'k'.repeat(2049) }, [], undefined, catalog, t).api_key,
    /settingsPanel\.models\.apiKeyTooLong/,
  );
});

test('reasoning validation uses the selected model capability rather than a frontend enum', () => {
  const modelCatalog = parseVendorCatalog({
    reasoning,
    token_plan: [
      preset({
        reasoning_capabilities: {
          'qwen-default': { openai: { options: ['off', 'on', 'new-tier'], recommended: 'on' } },
        },
      }),
    ],
    coding_plan: [],
    custom_api: [],
  });
  const draft = {
    alias: 'unique',
    protocol: 'openai',
    vendor_selection: 'token_plan:alibaba',
    model_name: 'qwen-default',
    model_input_mode: 'manual',
    api_key: 'secret',
    api_base: 'https://custom.example/v1',
    reasoning_level: 'extreme',
    context_window_tokens: '262144',
    is_default: false,
  };
  for (const level of ['extreme', 'low', 'medium', 'high']) {
    assert.match(
      validateModelDraft({ ...draft, reasoning_level: level }, [], undefined, modelCatalog, t).reasoning_level,
      /reasoningUnsupported/,
    );
  }
  for (const level of ['', 'off', 'on', 'new-tier']) {
    assert.equal(
      validateModelDraft({ ...draft, reasoning_level: level }, [], undefined, modelCatalog, t).reasoning_level,
      undefined,
    );
  }
  assert.equal(reconcileModelReasoning(draft, modelCatalog).reasoning_level, '');
  assert.equal(
    reconcileModelReasoning({ ...draft, reasoning_level: 'new-tier' }, modelCatalog).reasoning_level,
    'new-tier',
  );
  assert.equal(
    createModelDraft(
      { ...modelDraftToEntry(draft, undefined, modelCatalog, true), reasoning_level: 'historical' },
      modelCatalog,
    ).reasoning_level,
    'historical',
  );
});

test('Anthropic is rejected when the exact vendor preset does not support it', () => {
  const restrictedCatalog = parseVendorCatalog({
    reasoning,
    token_plan: [
      preset({
        supports_anthropic: false,
        anthropic_base: null,
        anthropic_client_provider: null,
      }),
    ],
    coding_plan: [],
    custom_api: [],
  });
  const draft = {
    ...applyVendorSelection(
      createModelDraft(undefined, restrictedCatalog),
      vendorSelectionKey('token_plan', 'alibaba'),
      restrictedCatalog,
    ),
    alias: 'restricted',
    protocol: 'anthropic',
    api_key: 'secret',
  };
  assert.match(validateModelDraft(draft, [], undefined, restrictedCatalog, t).protocol, /anthropicUnavailable/);
});

test('custom vendor uses server reasoning data even when there are no built-in presets', () => {
  const emptyCatalog = parseVendorCatalog({ reasoning, token_plan: [], coding_plan: [], custom_api: [] });
  const draft = {
    alias: 'custom-only',
    protocol: 'anthropic',
    vendor_selection: CUSTOM_VENDOR_SELECTION,
    model_name: 'custom-model',
    model_input_mode: 'manual',
    api_key: 'secret',
    api_base: 'https://custom.example/v1',
    reasoning_level: '',
    context_window_tokens: '262144',
    is_default: false,
  };
  assert.deepEqual(validateModelDraft(draft, [], undefined, emptyCatalog, t), {});
  const entry = modelDraftToEntry(draft, undefined, emptyCatalog, true);
  assert.equal(entry.model_provider, 'Anthropic');
  assert.equal(entry.api_base, 'https://custom.example/v1');
  assert.equal('vendor_key' in entry, false);
  assert.equal('plan' in entry, false);
  assert.match(
    validateModelDraft(draft, [], undefined, { ...emptyCatalog, reasoning: null }, t).reasoning_level,
    /reasoningUnavailable/,
  );
});

test('model listing preserves identity and read-only filtering', () => {
  const primary = { model_name: 'same', alias: 'first', is_default: true };
  const target = { model_name: 'same', alias: 'second', is_default: false };
  const other = { model_name: 'other', alias: 'third', is_default: true };
  const agentOs = { model_name: 'backup', alias: 'backup', is_agentos: true };
  const loginFree = {
    model_name: 'GLM-5.3',
    alias: 'GLM-5.3',
    is_free: true,
    source: 'huawei-maas-login',
    is_default: true,
  };
  const models = [loginFree, primary, target, other, agentOs];

  assert.deepEqual(getEditableModels(models), [primary, target, other]);
  assert.equal(
    getModelDisplayGroups(models).some((group) => group.items.some(({ model }) => model === loginFree)),
    false,
  );
  const displayGroups = getModelDisplayGroups([primary, other, target, agentOs]);
  assert.equal(displayGroups.length, 3);
  assert.deepEqual(
    displayGroups[0].items.map(({ model, index }) => [model.alias, index]),
    [
      ['first', 0],
      ['second', 2],
    ],
  );
  assert.deepEqual(
    displayGroups.slice(1).map((group) => group.items[0].model.alias),
    ['third', 'backup'],
  );
});

test('model Settings sources use the required RPCs without hardcoded vendor options or unsupported tiers', () => {
  const page = source('src/features/settings/modules/models/ModelsSettings.tsx');
  const definition = source('src/features/settings/modules/models/definition.ts');
  const tabLayout = source('src/features/settings/components/SettingsTabLayout.tsx');
  const dialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  const provider = source('src/features/settings/modules/models/ModelProviderSelect.tsx');
  const account = source('src/features/settings/modules/models/OpenAIAccountField.tsx');
  const accountCss = source('src/features/settings/modules/models/OpenAIAccountField.css');
  const operations = source('src/features/settings/modules/models/modelListOperations.ts');
  assert.match(page, /request<ModelResourceResult>\(\s*'models\.upsert'/);
  assert.match(page, /request\('models\.get', \{ model_id: nextDialog\.model\.model_id \}\)/);
  assert.doesNotMatch(page, /models\.references|MODEL_SELECTION_REFERENCED/);
  assert.match(page, /formatModelSelectionReferenceScopes|extractModelSelectionReferences/);
  assert.match(page, /request\('models\.delete', \{ model_id: confirmation\.model\.model_id \}/);
  assert.doesNotMatch(page, /models\.replace_all/);
  assert.match(page, /const refreshedPayload = await request\('models\.list'\)/);
  assert.match(page, /setAvailableModels\(parsed\.models, parsed\.activeModel\)/);
  assert.match(page, /const reloadModels = useCallback[\s\S]*await loadModels\(\)/);
  assert.match(page, /title=\{t\('settingsPanel\.models\.customModels'\)\}/);
  assert.match(page, /<FreeModelsSettings\s*\/>/);
  assert.match(page, /data-testid="settings-models-add-btn"/);
  assert.match(page, /<SettingsTabLayout/);
  assert.match(page, /testIdPrefix="settings-models"/);
  assert.match(page, /activeTab === 'models'/);
  assert.match(definition, /id: 'models', labelKey: 'settingsPanel\.models\.modelsTab'/);
  assert.match(definition, /id: 'groups', labelKey: 'settingsPanel\.models\.modelGroups'/);
  assert.match(definition, /id: 'jev', labelKey: 'settingsPanel\.models\.jevTab'/);
  assert.match(page, /activeTab === 'jev'/);
  assert.match(page, /<JevSettings disabled=\{disabled\}/);
  assert.match(definition, /defaultModelSettingsTab: ModelSettingsTabId = 'models'/);
  assert.match(tabLayout, /const \[activeTab, setActiveTab\] = useState<TabId>\(defaultTab\)/);
  assert.match(tabLayout, /items=\{tabs\.map\(\(tab\) => \(\{ value: tab\.id, label: t\(tab\.labelKey\) \}\)\)\}/);
  assert.match(page, /void loadCatalog\(\);[\s\S]{0,180}setDialog\(nextDialog\)/);
  assert.match(page, /type SaveModelsOptions = \{ errorScope\?: SettingsSaveErrorScope \};/);
  assert.match(page, /saveQueue\.enqueue\([\s\S]{0,320}\{ errorScope \},/);
  assert.match(page, /if \(errorScope === 'page'\) setSaveError\(message\)/);
  assert.match(
    page,
    /await saveModel\(next, dialog\.model \? 'model\.edit' : 'model\.add', \{ errorScope: 'caller' \}\)/,
  );
  assert.match(page, /customName: getModelDisplayName\(model\)/);
  assert.match(page, /metadata: formatModelMetadata\(model, t\)/);
  assert.match(page, /models\.map\(\(model, index\) => renderModelCard\(model, index\)\)/);
  assert.doesNotMatch(page, /getModelDisplayGroups|setGroupDefaultModel|settings-model-group"/);
  assert.match(
    page,
    /<h3[\s\S]{0,240}title=\{presentation\.customName\}[\s\S]{0,240}data-testid="settings-models-card-title"[\s\S]{0,240}>\s*\{presentation\.customName\}\s*<\/h3>/,
  );
  assert.match(
    page,
    /<p[\s\S]{0,240}title=\{presentation\.metadata\}[\s\S]{0,240}data-testid="settings-models-card-metadata"[\s\S]{0,240}>\s*\{presentation\.metadata\}\s*<\/p>/,
  );
  assert.match(page, /'model_groups\.upsert'/);
  assert.match(page, /'model_groups\.delete'/);
  assert.match(page, /setAvailableModelGroups\(parsed\.modelGroups\)/);
  assert.doesNotMatch(page, /accountMode|connectOpenAIAccount/);
  assert.doesNotMatch(dialog, /accountMode/);
  assert.doesNotMatch(page, /Promise\.all\(\[loadModels\(\), loadCatalog\(\)\]\)/);
  assert.doesNotMatch(page, /resolveModelPreset|flattenVendorCatalog/);
  assert.match(operations, /isRuntimeGrantedModel/);
  assert.match(operations, /model\.is_agentos !== true/);
  assert.doesNotMatch(page, /config\.save_all/);
  assert.match(dialog, /'vendors\.fetch_models'/);
  assert.match(dialog, /'config\.validate_model'/);
  assert.match(dialog, /name: 'protocol',[\s\S]{0,160}component: 'select'/);
  assert.doesNotMatch(dialog, /name: 'protocol',[\s\S]{0,160}component: 'radioGroup'/);
  assert.match(dialog, /catalogLoadFailed/);
  assert.doesNotMatch(dialog, /SUPPORTED_REASONING_LEVELS|settingsPanel\.models\.reasoning\.\$\{/);
  assert.match(dialog, /onRetryCatalog/);
  assert.match(dialog, /if \(Object\.keys\(errors\)\.length\) \{\s*form\.validate\(\);\s*return;/);
  assert.doesNotMatch(dialog, /confirmDisabled=\{[\s\S]*?Object\.keys\(errors\)\.length > 0/);
  assert.match(provider, /catalog\[group\]/);
  assert.match(provider, /kind: 'account', value: OPENAI_ACCOUNT_SELECTION, plan: 'other'/);
  assert.ok(provider.indexOf("kind: 'account'") < provider.indexOf("kind: 'custom'"));
  assert.match(provider, /useEffect\(\(\) => \{\s*if \(!open\) setQuery\(''\);\s*\}, \[open\]\)/);
  assert.doesNotMatch(provider, /useEffect\(\(\) => \{\s*if \(!open\) return;\s*setQuery\(''\)/);
  assert.match(dialog, /<OpenAIAccountSettings[\s\S]{0,260}onRequestLogout/);
  assert.match(dialog, /confirmDisabled=\{[\s\S]{0,300}account && openAIAccount\.busy/);
  assert.match(account, /OPENAI_ACCOUNT_RPC\.pendingLogin/);
  assert.match(account, /OPENAI_ACCOUNT_RPC\.startLogin/);
  assert.match(account, /OPENAI_ACCOUNT_RPC\.pollLogin/);
  assert.match(account, /OPENAI_ACCOUNT_RPC\.logout/);
  assert.match(account, /OPENAI_ACCOUNT_RPC\.listModels/);
  assert.match(account, /status\?\.auth_path[\s\S]{0,240}settings-oauth__auth-path[\s\S]{0,180}statusAuthPath/);
  assert.match(account, /busy: active && localBusy/);
  assert.doesNotMatch(account, /busy:[^\n]*(?:Boolean\(login\)|!authenticated)/);
  assert.doesNotMatch(account, /config\.openaiAccount\.description/);
  assert.doesNotMatch(account, /<strong>\{t\('config\.openaiAccount\.title'\)\}<\/strong>/);
  assert.doesNotMatch(account, /RefreshCw|refreshCoolingDown|refreshAuth/);
  assert.match(account, /statusRetryable[\s\S]{0,520}settingsPanel\.feedback\.retry/);
  assert.doesNotMatch(accountCss, /\.settings-oauth \{[\s\S]{0,240}background:/);
  assert.doesNotMatch(accountCss, /\.settings-oauth \{[\s\S]{0,240}border-radius:/);
  assert.doesNotMatch(provider, /SiliconFlow|InferenceAffinity|DashScope/);
  assert.doesNotMatch(dialog, /ultra|extreme/);
});

test('provider search is isolated from unrelated session-store updates', () => {
  const app = source('src/App.tsx');
  const form = source('src/components/form/components/Form.tsx');
  const websocket = source('src/hooks/useWebSocket.ts');
  const sessionStore = source('src/stores/sessionStore.ts');

  assert.match(
    app,
    /const \{\s*setCurrentSession,\s*setAvailableModels,\s*setAvailableModelGroups,\s*setMode,\s*setTeamLeaderMemberIds,?\s*\} = useSessionStore\.getState\(\)/,
  );
  assert.match(
    form,
    /const onBlur = useCallback\(\(\) => \{[\s\S]{0,180}trigger: 'blur'[\s\S]{0,80}\[form, item\.name\]\)/,
  );
  assert.doesNotMatch(websocket, /connectionStats|setConnectionStats|getInflightCount/);
  assert.doesNotMatch(sessionStore, /connectionStats|setConnectionStats/);
});

test('models are listed flat without same-name auto grouping', () => {
  const page = source('src/features/settings/modules/models/ModelsSettings.tsx');
  const groupsPage = source('src/features/settings/modules/models/ModelGroupsSettings.tsx');
  const settingsCss = source('src/features/settings/SettingsPage.css');
  const settingsAssets = source('src/assets/settings/index.ts');
  const zh = JSON.parse(source('src/i18n/locales/zh.json'));
  const en = JSON.parse(source('src/i18n/locales/en.json'));

  assert.match(page, /import \{ ModelGroupsSettings \} from '\.\/ModelGroupsSettings'/);
  assert.match(groupsPage, /import \{ CheckCircle2, ChevronDown \} from 'lucide-react'/);
  assert.doesNotMatch(groupsPage, /copyModelName|settings-models-group-route-copy-btn/);
  assert.match(groupsPage, /settings-models-group-route-test-btn/);
  assert.match(groupsPage, /if \(model\) onTestModel\(model, modelIndex\)/);
  assert.match(settingsAssets, /ConnectivityIcon from '\.\/actions\/connectivity\.svg\?react'/);
  assert.match(settingsAssets, /test: ConnectivityIcon/);
  assert.doesNotMatch(groupsPage, /ChevronRight|expandedModelGroups|getModelDisplayGroups|setGroupDefaultModel/);
  assert.match(page, /models\.map\(\(model, index\) => renderModelCard\(model, index\)\)/);
  assert.doesNotMatch(page, /setPrimary|isPrimary|canSetPrimary|settings-models-card-primary-tag/);
  assert.doesNotMatch(page, /settings-model-group__header|groupOrdinal|isDuplicate/);
  assert.doesNotMatch(settingsCss, /\.settings-model-group\s*\{/);
  assert.match(settingsCss, /\.settings-models-group-card\s*\{/);
  assert.match(settingsCss, /\.settings-models-group-route\s*\{/);
  assert.match(groupsPage, /settings-models-group-card__toggle/);
  assert.match(page, /promoteGroupRouteDefault/);
  assert.match(groupsPage, /settingsPanel\.models\.modelGroupsManagement/);
  assert.equal(zh.settingsPanel.models.modelGroups, '模型组');
  assert.equal(en.settingsPanel.models.modelGroups, 'Model groups');
  assert.equal(zh.settingsPanel.models.modelGroupsManagement, '模型组管理');
  assert.equal(en.settingsPanel.models.modelGroupsManagement, 'Model group management');
  assert.equal(zh.settingsPanel.models.modelGroupRouteDefault, '组内默认');
  assert.equal(en.settingsPanel.models.modelGroupRouteDefault, 'Group default');
  assert.equal(zh.settingsPanel.models.groupMeta, undefined);
  assert.equal(en.settingsPanel.models.groupMeta, undefined);
});

test('model dialog uses provider terminology and validates as part of confirmation', () => {
  const dialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  const modelNameField = source('src/features/settings/modules/models/ModelNameField.tsx');
  const provider = source('src/features/settings/modules/models/ModelProviderSelect.tsx');
  const form = source('src/components/form/components/Form.tsx');
  const confirmDialog = source('src/features/settings/components/SettingsConfirmDialog.tsx');
  const button = source('src/components/ui/Button/Button.tsx');
  const buttonCss = source('src/components/ui/Button/Button.css');
  const settingsCss = source('src/features/settings/SettingsPage.css');
  const zh = JSON.parse(source('src/i18n/locales/zh.json'));
  const en = JSON.parse(source('src/i18n/locales/en.json'));

  assert.match(
    dialog,
    /const validateAndSave = async \(\) => \{[\s\S]*const snapshot = buildEntry\(\);[\s\S]*buildModelValidationPayload\(snapshot\)[\s\S]*await persist\(snapshot\)/,
  );
  assert.match(dialog, /const persist = async \(snapshot: ModelEntry\)[\s\S]*await onSave\(snapshot\)/);
  assert.match(
    dialog,
    /setValidationFailure\(\{[\s\S]{0,220}snapshot,[\s\S]*const \{ snapshot \} = validationFailure;[\s\S]*void persist\(snapshot\)/,
  );
  assert.match(dialog, /onConfirm=\{\(\) => void validateAndSave\(\)\}/);
  assert.match(dialog, /<SettingsConfirmDialog[\s\S]*validationBeforeSaveTitle[\s\S]*continueSave/);
  assert.match(dialog, /confirmVariant="warning"/);
  assert.match(dialog, /disabled=\{testing \|\| submitting \|\| saving\}/);
  assert.match(dialog, /settingsPanel\.models\.testingConnection[\s\S]*settingsPanel\.models\.savingModel/);
  assert.match(confirmDialog, /confirmVariant\?: ButtonProps\['variant'\]/);
  assert.match(confirmDialog, /<Button variant=\{confirmVariant\}/);
  assert.match(button, /'primary' \| 'secondary' \| 'quiet' \| 'warning' \| 'danger'/);
  assert.match(buttonCss, /\.ui-button--warning[\s\S]*--color-feedback-warning-subtle/);
  assert.doesNotMatch(settingsCss, /\.settings-page \.ui-button--warning\s*\{/);
  assert.doesNotMatch(dialog, /secondaryAction=|formDescription/);
  assert.ok(dialog.indexOf("name: 'vendor_selection'") < dialog.indexOf("name: 'protocol'"));
  assert.ok(dialog.indexOf("name: 'protocol'") < dialog.indexOf("name: 'api_base'"));
  assert.ok(dialog.indexOf("name: 'api_base'") < dialog.indexOf("name: 'api_key'"));
  assert.ok(dialog.indexOf("name: 'api_key'") < dialog.indexOf("name: 'model_name'"));
  assert.ok(dialog.indexOf("name: 'model_name'") < dialog.indexOf("name: 'alias'"));
  assert.ok(dialog.indexOf("name: 'alias'") < dialog.indexOf("name: 'context_window_tokens'"));
  assert.ok(dialog.indexOf("name: 'context_window_tokens'") < dialog.indexOf("name: 'reasoning_level'"));
  assert.match(
    dialog,
    /name: 'model_name'[\s\S]{0,400}onBlur:[\s\S]{0,220}syncDisplayNameWithModelName/,
  );
  assert.doesNotMatch(
    dialog,
    /name: 'model_name'[\s\S]{0,220}onChange:[\s\S]{0,180}syncDisplayNameWithModelName/,
  );
  assert.doesNotMatch(dialog, /updateModelMode|enterModelId|chooseListedModel/);
  assert.match(dialog, /fetchedModelLists\.current\.has\(fetchKey\)/);
  assert.match(dialog, /fetchedModelLists\.current\.clear\(\)/);
  assert.match(dialog, /setFetchStatus\(t\('settingsPanel\.models\.fetchModelsLoading'\)\)/);
  assert.match(dialog, /result\.source === 'preset'[\s\S]{0,180}updateModelOptions\(\[\]\)/);
  assert.match(dialog, /const updateModelOptions = \(options: readonly string\[\]\)[\s\S]{0,160}setModelOptions/);
  assert.doesNotMatch(dialog, /const updateModelOptions[\s\S]{0,220}form\.setFieldValue\('model_name'/);
  assert.match(dialog, /syncDisplayNameWithModelName/);
  assert.match(dialog, /form\.validate\('alias'\)/);
  assert.match(dialog, /if \(Object\.keys\(errors\)\.length\)/);
  assert.match(dialog, /account \? !openAIAccount\.authenticated : !model && !values\.vendor_selection/);
  assert.doesNotMatch(dialog, /historicalAliasRequired|history-warning|aliasManuallyEdited/);
  assert.equal(zh.settingsPanel.models.customName, '模型展示名称');
  assert.equal(zh.settingsPanel.models.validation.aliasRequired, '模型展示名称为必填项');
  assert.equal(
    zh.settingsPanel.models.validation.aliasConflict,
    '模型展示名称必须全局唯一，不能与其他模型的展示名称重复',
  );
  assert.equal(en.settingsPanel.models.customName, 'Model display name');
  assert.equal(en.settingsPanel.models.validation.aliasRequired, 'Model display name is required');
  assert.match(modelNameField, /className="settings-model-name-field__refresh"/);
  assert.match(modelNameField, /className="settings-model-name-field__toggle"/);
  assert.match(
    modelNameField,
    /const refreshCompleted = wasFetching\.current && !fetching;[\s\S]{0,260}setQuery\(''\);[\s\S]{0,180}setOpen\(true\)/,
  );
  assert.match(modelNameField, /createPortal/);
  assert.match(
    modelNameField,
    /setPortalHost\(rootRef\.current\?\.closest\('dialog'\) \?\? document\.body\);\s*\}, \[mode\]\)/,
  );
  assert.match(modelNameField, /const openBelow = spaceBelow >= Math\.min\(desiredHeight, spaceAbove\)/);
  assert.match(modelNameField, /top: rect\.bottom \+ gap/);
  assert.match(modelNameField, /bottom: window\.innerHeight - rect\.top \+ gap/);
  assert.match(modelNameField, /data-empty=\{filteredOptions\.length === 0 \|\| undefined\}/);
  assert.match(modelNameField, /className="settings-model-name-field__empty" role="status"/);
  assert.match(form, /item\.labelAction[\s\S]*form-item__heading-row/);
  assert.match(provider, /settingsPanel\.models\.vendorPlaceholder/);
  assert.match(provider, /title=\{selectedApiAddress \|\| undefined\}/);
  assert.equal(zh.settingsPanel.models.vendor, '模型提供商');
  assert.equal(zh.settingsPanel.models.vendorPlaceholder, '请选择');
  assert.equal(zh.settingsPanel.models.apiKeyLabel, 'API Key');
  assert.equal(zh.settingsPanel.models.apiKeyTooLong, 'API Key 过长，请控制在 2048 字符以内');
  assert.equal(zh.settingsPanel.models.fetchModelsLoading, '获取中...');
  assert.equal(zh.settingsPanel.models.testingConnection, '正在测试连接…');
  assert.equal(zh.settingsPanel.models.savingModel, '正在保存…');
  assert.equal(zh.settingsPanel.models.continueSave, '忽略测试并保存');
  assert.equal(en.settingsPanel.models.vendor, 'Model provider');
  assert.equal(en.settingsPanel.models.vendorPlaceholder, 'Select');
  assert.equal(en.settingsPanel.models.apiKeyTooLong, 'API key is too long. Use 2048 characters or fewer');
  assert.equal(en.settingsPanel.models.fetchModelsLoading, 'Fetching...');
  assert.equal(en.settingsPanel.models.testingConnection, 'Testing connection…');
  assert.equal(en.settingsPanel.models.savingModel, 'Saving…');
  assert.equal(en.settingsPanel.models.continueSave, 'Ignore test and save');
});
