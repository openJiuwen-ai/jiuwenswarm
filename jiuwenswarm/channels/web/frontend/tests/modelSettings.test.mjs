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
  vendorSelectionKey,
} from '../node_modules/.cache/settings-refactor/modules/models/modelAdapters.js';
import { validateModelDraft } from '../node_modules/.cache/settings-refactor/modules/models/modelValidation.js';
import {
  getEditableModels,
  getModelDisplayGroups,
  promotePrimaryModel,
  removeEditableModel,
  setGroupDefaultModel,
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
  assert.equal(draft.model_name, 'qwen-default');
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

test('editing a model preserves an empty custom name without deriving it from the model name', () => {
  const legacyModel = {
    alias: '',
    model_name: 'qwen-existing',
    api_base: 'https://openai.example/v1',
    api_key: '',
    model_provider: 'OpenAI',
    vendor_key: 'alibaba',
    plan: 'token_plan',
  };
  assert.equal(createModelDraft(legacyModel, catalog).alias, '');
  assert.equal(createModelDraft({ ...legacyModel, alias: 'my-qwen' }, catalog).alias, 'my-qwen');

  const dialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  assert.doesNotMatch(dialog, /aliasManuallyEdited|syncAliasWithModel/);
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

test('alias validation is optional, global, exact, and excludes the edited row only by origin_index', () => {
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
    is_default: false,
  };
  assert.equal(validateModelDraft(baseDraft, models, undefined, catalog, t).alias, undefined);
  assert.match(validateModelDraft({ ...baseDraft, alias: 'second' }, models, 3, catalog, t).alias, /aliasConflict/);
  assert.match(
    validateModelDraft({ ...baseDraft, alias: 'same-model' }, models, undefined, catalog, t).alias,
    /aliasConflict/,
  );
  assert.equal(validateModelDraft({ ...baseDraft, alias: 'first' }, models, 3, catalog, t).alias, undefined);
  assert.equal(
    validateModelDraft({ ...baseDraft, model_name: 'same-model' }, models, undefined, catalog, t).model_name,
    undefined,
  );
  assert.match(
    validateModelDraft({ ...baseDraft, alias: 'unique', model_name: 'second' }, models, undefined, catalog, t)
      .model_name,
    /modelNameConflict/,
  );
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

test('default and deletion operations preserve identity, group semantics, and read-only filtering', () => {
  const primary = { model_name: 'same', alias: 'first', is_default: true };
  const target = { model_name: 'same', alias: 'second', is_default: false };
  const other = { model_name: 'other', alias: 'third', is_default: true };
  const agentOs = { model_name: 'backup', alias: 'backup', is_agentos: true };
  const free = { model_name: 'free', alias: 'free', is_free: true };
  const models = [primary, target, other, agentOs, free];

  assert.deepEqual(getEditableModels(models), [primary, target, other]);
  const displayGroups = getModelDisplayGroups([primary, other, target, agentOs, free]);
  assert.equal(displayGroups.length, 4);
  assert.deepEqual(
    displayGroups[0].items.map(({ model, index }) => [model.alias, index]),
    [
      ['first', 0],
      ['second', 2],
    ],
  );
  assert.deepEqual(
    displayGroups.slice(1).map((group) => group.items[0].model.alias),
    ['third', 'backup', 'free'],
  );
  const promoted = promotePrimaryModel(models, target);
  assert.equal(promoted.length, models.length);
  assert.equal(promoted[0].alias, 'second');
  assert.equal(promoted[0].is_default, true);
  assert.equal(promoted.filter((model) => model.model_name === 'same' && model.is_default).length, 1);
  const groupDefault = setGroupDefaultModel(models, target);
  assert.equal(groupDefault.length, models.length);
  assert.equal(groupDefault[0].alias, 'second');
  assert.equal(groupDefault.filter((model) => model.model_name === 'same' && model.is_default).length, 1);
  assert.throws(() => removeEditableModel([primary, agentOs, free], primary), /LAST_EDITABLE_MODEL/);
});

test('model Settings sources use the required RPCs without hardcoded vendor options or unsupported tiers', () => {
  const page = source('src/features/settings/modules/models/ModelsSettings.tsx');
  const dialog = source('src/features/settings/modules/models/ModelDialog.tsx');
  const provider = source('src/features/settings/modules/models/ModelProviderSelect.tsx');
  const account = source('src/features/settings/modules/models/OpenAIAccountField.tsx');
  const accountCss = source('src/features/settings/modules/models/OpenAIAccountField.css');
  const operations = source('src/features/settings/modules/models/modelListOperations.ts');
  assert.match(page, /request<ReplaceModelsResult>\(\s*'models\.replace_all'/);
  assert.match(page, /const refreshedPayload = await request\('models\.list'\)/);
  assert.match(page, /setAvailableModels\(parsed\.models, parsed\.activeModel\)/);
  assert.match(page, /const reloadModels = useCallback[\s\S]*await loadModels\(\)/);
  assert.match(
    page,
    /<SettingsSection\s+title=\{t\('settingsPanel\.models\.primaryModels'\)\}\s+separatedRows\s+action=\{\s*<Button variant="primary"[\s\S]{0,180}settingsPanel\.models\.addModel[\s\S]{0,40}<\/Button>\s*\}/,
  );
  assert.match(page, /setDialog\(nextDialog\);\s*void loadCatalog\(\)/);
  assert.match(page, /type SaveModelsOptions = \{ errorScope\?: SettingsSaveErrorScope \};/);
  assert.match(page, /saveQueue\.enqueue\([\s\S]{0,320}\{ errorScope \},/);
  assert.match(page, /if \(errorScope === 'page'\) setSaveError\(message\)/);
  assert.match(
    page,
    /await saveModels\(nextModels, dialog\.model \? 'model\.edit' : 'model\.add', \{ errorScope: 'caller' \}\)/,
  );
  assert.match(page, /const baseName = model\.alias\?\.trim\(\) \|\| model\.model_name/);
  assert.match(page, /`\$\{baseName\} #\$\{groupOrdinal\}`/);
  assert.match(page, /modelDisplayGroups\.map\(\(group, displayGroupIndex\)/);
  assert.match(page, /className="settings-model-group"/);
  assert.match(page, /renderModelCard\(item\.model, item\.index, group\.items\.indexOf\(item\) \+ 1\)/);
  assert.match(page, /const protocol = displayModelProtocol\(model\)/);
  assert.match(page, /vendorKey \? getVendorLabel\(vendorKey, t\) : t\('settingsPanel\.models\.customVendor'\)/);
  assert.match(page, /\[providerLabel, protocolLabel, model\.model_name\]\.join\(' · '\)/);
  assert.match(page, /<h3 title=\{presentation\.customName\}>\{presentation\.customName\}<\/h3>/);
  assert.match(page, /<p title=\{presentation\.metadata\}>\{presentation\.metadata\}<\/p>/);
  assert.doesNotMatch(page, /accountMode|connectOpenAIAccount/);
  assert.doesNotMatch(dialog, /accountMode/);
  assert.doesNotMatch(page, /Promise\.all\(\[loadModels\(\), loadCatalog\(\)\]\)/);
  assert.doesNotMatch(page, /resolveModelPreset|flattenVendorCatalog/);
  assert.match(operations, /model\.is_free !== true && model\.is_agentos !== true/);
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
    /const \{\s*setCurrentSession,\s*setAvailableModels,\s*setMode,\s*setTeamLeaderMemberIds,?\s*\} = useSessionStore\.getState\(\)/,
  );
  assert.match(
    form,
    /const onBlur = useCallback\(\(\) => \{[\s\S]{0,180}trigger: 'blur'[\s\S]{0,80}\[form, item\.name\]\)/,
  );
  assert.doesNotMatch(websocket, /connectionStats|setConnectionStats|getInflightCount/);
  assert.doesNotMatch(sessionStore, /connectionStats|setConnectionStats/);
});

test('grouped models use an accessible accordion and keep only the group default visible when collapsed', () => {
  const page = source('src/features/settings/modules/models/ModelsSettings.tsx');
  const settingsCss = source('src/features/settings/SettingsPage.css');
  const zh = JSON.parse(source('src/i18n/locales/zh.json'));
  const en = JSON.parse(source('src/i18n/locales/en.json'));

  assert.match(page, /import \{ Check, ChevronRight \} from 'lucide-react'/);
  assert.match(
    page,
    /const \[expandedModelGroups, setExpandedModelGroups\] = useState<Record<string, boolean>>\(\{\}\)/,
  );
  assert.match(page, /const defaultItem = group\.items\.find\(\(\{ model \}\) => model\.is_default === true\)!/);
  assert.match(page, /const alternativeItems = group\.items\.filter\(\(item\) => item !== defaultItem\)/);
  assert.match(
    page,
    /const canSetPrimary = !readOnly && !isPrimary && \(!isDuplicate \|\| model\.is_default === true\)/,
  );
  assert.match(page, /\{canSetPrimary \? \(\s*<Button[\s\S]{0,320}settingsPanel\.models\.setPrimary/);
  assert.match(
    page,
    /<button[\s\S]{0,240}className="settings-model-group__header"[\s\S]{0,240}aria-expanded=\{isExpanded\}[\s\S]{0,160}aria-controls=\{groupContentId\}/,
  );
  assert.match(page, /renderModelCard\(defaultItem\.model, defaultItem\.index, defaultOrdinal\)/);
  assert.match(page, /className="settings-model-group__alternatives"[\s\S]{0,80}hidden=\{!isExpanded\}/);
  assert.match(page, /settings-model-group__toggle-icon--expanded/);
  assert.match(
    page,
    /<strong title=\{group\.modelName\}>\{group\.modelName\}<\/strong>\s*<\/div>\s*<span className="settings-model-group__meta">[\s\S]{0,120}settingsPanel\.models\.groupMeta/,
  );
  assert.doesNotMatch(page, /settings-model-group__count/);
  assert.match(settingsCss, /\.settings-model-group__header\s*\{[^}]*justify-content: space-between/s);
  assert.match(settingsCss, /\.settings-model-group__header:hover\s*\{[^}]*var\(--color-surface-hover\)/s);
  assert.match(settingsCss, /\.settings-model-group__toggle-icon--expanded\s*\{[^}]*rotate\(90deg\)/s);
  assert.match(settingsCss, /\.settings-model-group > \.settings-model-card--grouped,/);
  assert.match(settingsCss, /\.settings-model-group__alternatives\s*\{[^}]*var\(--color-surface-card\)/s);
  assert.equal(zh.settingsPanel.models.groupMeta, '模型组 · {{count}} 个配置');
  assert.equal(en.settingsPanel.models.groupMeta, 'Model group · {{count}} configurations');
  assert.doesNotMatch(page, /settings-model-group__alternatives-heading/);
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
  assert.ok(dialog.indexOf("name: 'alias'") < dialog.indexOf("name: 'reasoning_level'"));
  assert.doesNotMatch(dialog, /updateModelMode|enterModelId|chooseListedModel/);
  assert.match(dialog, /fetchedModelLists\.current\.has\(fetchKey\)/);
  assert.match(dialog, /fetchedModelLists\.current\.clear\(\)/);
  assert.match(dialog, /setFetchStatus\(t\('settingsPanel\.models\.fetchModelsLoading'\)\)/);
  assert.match(dialog, /result\.source === 'preset'[\s\S]{0,180}updateModelOptions\(nextOptions\)/);
  assert.match(dialog, /const updateModelOptions = \(options: readonly string\[\]\)[\s\S]{0,160}setModelOptions/);
  assert.doesNotMatch(dialog, /const updateModelOptions[\s\S]{0,220}form\.setFieldValue\('model_name'/);
  assert.doesNotMatch(dialog, /aliasManuallyEdited|syncAliasWithModel|form\.validate\('alias'\)/);
  assert.match(dialog, /account \? !openAIAccount\.authenticated : !model && !values\.vendor_selection/);
  assert.doesNotMatch(dialog, /historicalAliasRequired|history-warning/);
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
