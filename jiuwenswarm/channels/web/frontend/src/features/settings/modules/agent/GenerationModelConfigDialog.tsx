import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { VendorFetchModelsResult, VendorPreset, VendorPresetMap } from '../../../../types';
import { Button } from '../../../../components/ui';
import { Form, FormDialog, useForm, useFormState, type FormItem } from '../../../../components/form';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { ModelNameField } from '../models/ModelNameField';
import { ModelProviderSelect } from '../models/ModelProviderSelect';
import {
  CUSTOM_VENDOR_SELECTION,
  findVendorPreset,
  parseVendorCatalog,
  vendorSelectionKey,
} from '../models/modelAdapters';
import {
  catalogProtocols,
  credentialsOptional,
  defaultGeneration,
  generationApiBase,
  generationModelOptions,
  generationProviderName,
  generationVendor,
  isArkGenerationApiBase,
  isEditableBaseVendor,
  MODELARK_PROTOCOL,
  modelProtocol,
  regionalHintKey,
  VLLM_OMNI_PROTOCOL,
  withCurrentOption,
  type GenerationSlot,
} from './generationModels';

const EMPTY_VENDOR_CATALOG: VendorPresetMap = {
  reasoning: null,
  token_plan: [],
  coding_plan: [],
  custom_api: [],
};

/** 视频/图像生成配置弹窗里"API"分组只保留有生成目录的厂商。其余厂商没有可用的
 *  视频/图像生成模型，放出来只会诱导用户选到一个填不出正确模型名的死路。已保存的
 *  旧配置仍然用完整的 catalog（见下方 preset/findStoredPreset）正常回显，这里只是
 *  收窄下拉本身能新选到的选项。百炼和 vLLM-Omni 的目录在 design 侧，不能被上游
 *  只含 OpenRouter / 火山引擎 / MiniMax 的白名单滤掉。 */
const ALLOWED_API_VENDORS = new Set(['openrouter', 'volcengine', 'minimax', 'alibaba', 'vllm-omni']);

const OPENAI_PROTOCOL = 'openai';

type SaveConfig = (updates: Record<string, string>, operation: string) => Promise<unknown>;

type GenerationDraft = {
  vendor_selection: string;
  protocol: string;
  api_base: string;
  api_key: string;
  model_name: string;
};

function readConfig(config: Readonly<Record<string, unknown>>, slot: GenerationSlot, suffix: string): string {
  return String(config[`${slot}_${suffix}`] ?? '');
}

function trimSlash(value: string): string {
  return value.trim().replace(/\/+$/, '').toLowerCase();
}

/** 之前保存下来的配置只有 provider 名称、协议和 api_base（没有厂商 key）。vLLM-Omni 的地址
 *  由用户填写，按协议找回；其余按 api_base 优先、其次按名称找回对应的厂商预设，这样编辑
 *  已有配置时下拉能回显成对应厂商。
 *
 *  只在 custom_api 里找：这个弹窗的 ModelProviderSelect 固定只展示 custom_api（见下方
 *  groups={['custom_api', 'other']}），有的厂商（如 MiniMax）在 token_plan/coding_plan
 *  下也有一份同 api_base 的预设 —— flattenVendorCatalog 会先命中那份，算出的
 *  vendor_selection（"token_plan:minimax"）在下拉的选项里根本不存在，回显成了"请选择"。 */
function findStoredPreset(
  catalog: VendorPresetMap,
  provider: string,
  protocol: string,
  apiBase: string,
): VendorPreset | undefined {
  const presets = catalog.custom_api;
  if (protocol.trim() === VLLM_OMNI_PROTOCOL) {
    return presets.find((preset) => preset.vendor_key === VLLM_OMNI_PROTOCOL);
  }
  const base = trimSlash(apiBase);
  const name = provider.trim().toLowerCase();
  const arkPreset = catalog.custom_api.find((preset) => preset.vendor_key === 'volcengine');
  if (
    arkPreset &&
    (isArkGenerationApiBase(apiBase) || name === arkPreset.vendor_key || name === arkPreset.display_name.toLowerCase())
  ) {
    return arkPreset;
  }
  return (
    (base
      ? presets.find((preset) => trimSlash(generationApiBase(preset.vendor_key, preset.api_base)) === base)
      : undefined) ??
    (name
      ? presets.find((preset) => preset.display_name.toLowerCase() === name || preset.vendor_key.toLowerCase() === name)
      : undefined)
  );
}

function createDraft(config: Readonly<Record<string, unknown>>, slot: GenerationSlot): GenerationDraft {
  const apiBase = readConfig(config, slot, 'api_base');
  const apiKey = readConfig(config, slot, 'api_key');
  const model = readConfig(config, slot, 'model');
  const provider = readConfig(config, slot, 'provider');
  const hasConfig = [apiBase, apiKey, model, provider].some((value) => value.trim());
  return {
    // 目录还没加载出来时先当成自定义，目录到了再回显成具体厂商（见 useEffect）。
    vendor_selection: hasConfig ? CUSTOM_VENDOR_SELECTION : '',
    protocol: readConfig(config, slot, 'protocol') || modelProtocol(slot, undefined, true, model),
    api_base: apiBase,
    api_key: apiKey,
    model_name: model,
  };
}

function buildUpdates(slot: GenerationSlot, draft: GenerationDraft, catalog: VendorPresetMap): Record<string, string> {
  const preset = findVendorPreset(catalog, draft.vendor_selection);
  const editableBase = !preset || isEditableBaseVendor(preset.vendor_key);
  return {
    [`${slot}_provider`]: generationProviderName(preset?.vendor_key, preset?.client_provider),
    [`${slot}_protocol`]: draft.protocol.trim(),
    [`${slot}_api_base`]: (editableBase ? draft.api_base : preset.api_base).trim(),
    [`${slot}_api_key`]: draft.api_key.trim(),
    [`${slot}_model`]: draft.model_name.trim(),
  };
}

export function GenerationModelConfigDialog({
  slot,
  titleKey,
  config,
  enableOnSave,
  save,
  onClose,
}: {
  slot: GenerationSlot;
  titleKey: string;
  config: Record<string, unknown>;
  enableOnSave: boolean;
  save: SaveConfig;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const initialValues = useMemo(() => createDraft(config, slot), [config, slot]);
  const form = useForm<GenerationDraft>({ initialValues });
  useFormState(form);
  const values = form.getValues();
  const [catalog, setCatalog] = useState<VendorPresetMap>(EMPTY_VENDOR_CATALOG);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [saveError, setSaveError] = useState('');
  const catalogRequestId = useRef(0);
  const fetchRequestId = useRef(0);
  const [fetchingKey, setFetchingKey] = useState<string | null>(null);
  const [fetchedModels, setFetchedModels] = useState<{ key: string; models: string[] } | null>(null);
  const [fetchStatus, setFetchStatus] = useState<{ key: string; text: string } | null>(null);
  const [attemptedFetchKey, setAttemptedFetchKey] = useState<string | null>(null);
  // 只给下拉本身用的收窄版 catalog——preset/findStoredPreset 等逻辑仍然用上面
  // 完整的 catalog，不然已保存的旧配置（provider 是被收窄掉的厂商）编辑时会
  // 回显失败。
  const pickerCatalog = useMemo<VendorPresetMap>(
    () => ({ ...catalog, custom_api: catalog.custom_api.filter((preset) => ALLOWED_API_VENDORS.has(preset.vendor_key)) }),
    [catalog],
  );
  const closeBlocked = submitting;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: `agent-${slot}-model-dialog`,
    form,
    closeBlocked,
    onClose,
  });

  const preset = findVendorPreset(catalog, values.vendor_selection);
  const custom = values.vendor_selection === CUSTOM_VENDOR_SELECTION;
  // API 地址：自定义、密钥分区域的厂商（MiniMax / 火山引擎 / 百炼）和自部署厂商（vLLM-Omni）让用户填/改；
  // 其余厂商用预设地址，不展示。
  const showApiBase = custom || isEditableBaseVendor(preset?.vendor_key);
  const apiBase = (showApiBase ? values.api_base : (preset?.api_base ?? values.api_base)).trim();
  // 用哪份模型目录：OpenRouter / MiniMax 有各自的目录；自定义地址给出全部作为候选
  // （可能是兼容它们的代理），仍允许手输别的 ID；其他厂商没有已知的生成模型，只能手输。
  const vendor = generationVendor(preset?.vendor_key, custom ? '' : apiBase);
  const catalogAll = custom;
  const catalogApplies = catalogAll || vendor !== undefined;
  const arkSelection = vendor === 'modelark' && preset?.vendor_key === 'volcengine' && preset.plan === 'custom_api';
  const fetchKey = JSON.stringify([slot, values.vendor_selection, apiBase, values.api_key.trim()]);

  const fetchGenerationModels = async () => {
    const current = form.getValues();
    const selected = findVendorPreset(catalog, current.vendor_selection);
    if (!isConnected || selected?.vendor_key !== 'volcengine' || selected.plan !== 'custom_api') return;
    const base = current.api_base.trim();
    const key = current.api_key.trim();
    const identity = JSON.stringify([slot, current.vendor_selection, base, key]);
    if (!key) {
      setFetchStatus({ key: identity, text: t('settingsPanel.models.fetchReasons.apiKeyRequired') });
      return;
    }
    const currentRequestId = ++fetchRequestId.current;
    setAttemptedFetchKey(identity);
    setFetchingKey(identity);
    setFetchStatus({ key: identity, text: t('settingsPanel.models.fetchModelsLoading') });
    try {
      const result = await request<VendorFetchModelsResult>(
        'vendors.fetch_models',
        { vendor_key: 'volcengine', plan: 'custom_api', api_key: key, api_base: base, modality: slot === 'visual_gen' ? 'image' : 'video' },
        { timeoutMs: 30_000 },
      );
      if (currentRequestId !== fetchRequestId.current) return;
      if (result.source !== 'remote' || !Array.isArray(result.models) || !result.models.every((name) => typeof name === 'string')) {
        throw new Error(result.reason || t('settingsPanel.models.fetchModelsInvalidResponse'));
      }
      const models = Array.from(new Set(result.models.map((name) => name.trim()).filter(Boolean)));
      setFetchedModels({ key: identity, models });
      setFetchStatus({ key: identity, text: t('settingsPanel.models.fetchGenerationModelsRemote', { count: models.length }) });
    } catch (error) {
      if (currentRequestId === fetchRequestId.current) {
        const detail = error instanceof Error ? error.message : t('settingsPanel.models.fetchModelsFailed');
        setFetchStatus({ key: identity, text: detail });
      }
    } finally {
      if (currentRequestId === fetchRequestId.current) setFetchingKey(null);
    }
  };

  const loadCatalog = useCallback(async () => {
    if (!isConnected) {
      setCatalogLoading(false);
      return;
    }
    const currentRequestId = ++catalogRequestId.current;
    setCatalog(EMPTY_VENDOR_CATALOG);
    setCatalogLoading(true);
    setCatalogError('');
    try {
      const payload = await request<{ vendors?: unknown }>('vendors.list');
      if (currentRequestId !== catalogRequestId.current) return;
      setCatalog(parseVendorCatalog(payload.vendors));
    } catch (error) {
      if (currentRequestId === catalogRequestId.current) {
        setCatalogError(
          error instanceof Error && error.message !== 'INVALID_VENDOR_CATALOG'
            ? error.message
            : t('settingsPanel.models.catalogResponseInvalid'),
        );
      }
    } finally {
      if (currentRequestId === catalogRequestId.current) setCatalogLoading(false);
    }
  }, [isConnected, request, t]);

  useEffect(() => {
    void loadCatalog();
    return () => {
      catalogRequestId.current += 1;
    };
  }, [loadCatalog]);

  // 编辑已有配置：目录加载完成后，把"自定义"回显成实际对应的厂商。没动过表单时
  // 用 reset 一并更新基线，避免这次程序性的回显被算成"有未保存的修改"。
  useEffect(() => {
    const current = form.getValues();
    if (current.vendor_selection !== CUSTOM_VENDOR_SELECTION) return;
    // Search pickerCatalog, not the full catalog: that's the exact (ALLOWED_API_VENDORS-filtered,
    // custom_api-only) set ModelProviderSelect offers below, so whatever this finds is guaranteed
    // to be a selectable option rather than a same-api_base preset from a plan the picker hides.
    const match = findStoredPreset(pickerCatalog, readConfig(config, slot, 'provider'), current.protocol, current.api_base);
    if (!match) return;
    const next = { ...current, vendor_selection: vendorSelectionKey(match.plan, match.vendor_key) };
    // 没有专属目录的厂商只支持 OpenAI 协议，旧配置里残留的其他协议值一并纠正。
    if (!generationVendor(match.vendor_key, match.api_base)) next.protocol = OPENAI_PROTOCOL;
    if (form.hasUnsavedChanges()) form.setValues({ vendor_selection: next.vendor_selection });
    else form.reset(next);
  }, [pickerCatalog, config, form, slot]);

  const updateVendor = (selection: string) => {
    if (selection === CUSTOM_VENDOR_SELECTION) {
      form.setValues({
        vendor_selection: selection,
        api_base: '',
        api_key: '',
        model_name: '',
        protocol: defaultGeneration(slot, undefined).protocol,
      });
      form.clearValidate(['vendor_selection', 'api_base', 'api_key', 'model_name', 'protocol']);
      return;
    }
    const nextPreset = findVendorPreset(catalog, selection);
    if (!nextPreset) {
      form.setFieldValue('vendor_selection', selection);
      return;
    }
    const nextVendor = generationVendor(nextPreset.vendor_key, nextPreset.api_base);
    const defaults = defaultGeneration(slot, nextVendor);
    form.setValues({
      vendor_selection: selection,
      api_base: generationApiBase(nextPreset.vendor_key, nextPreset.api_base),
      api_key: '',
      protocol: nextVendor ? defaults.protocol : OPENAI_PROTOCOL,
      model_name: nextVendor === 'modelark' ? '' : defaults.model,
    });
    form.clearValidate(['vendor_selection', 'api_base', 'api_key', 'model_name', 'protocol']);
  };

  // MiniMax / OpenRouter / 火山引擎 / 自定义 按各自目录给出协议；其余厂商只有 OpenAI 一种协议。
  const openAIOnly = !custom && Boolean(values.vendor_selection) && vendor === undefined;
  const protocolOptions = openAIOnly
    ? [{ value: OPENAI_PROTOCOL, label: t('settingsPanel.models.protocols.openai') }]
    : withCurrentOption(catalogProtocols(slot, vendor, catalogAll), values.protocol).map((value) => ({
        value,
        label: value,
      }));
  const modelOptions = arkSelection
    ? withCurrentOption(fetchedModels?.key === fetchKey ? fetchedModels.models : [], values.model_name)
    : catalogApplies
      ? generationModelOptions(slot, vendor, catalogAll, values.protocol, values.model_name)
      : [];

  const optionalCredentials = credentialsOptional(values.protocol);

  const errors = useMemo(() => {
    const next: Partial<Record<keyof GenerationDraft, string>> = {};
    const baseValue = values.api_base.trim();
    if (!values.vendor_selection) {
      next.vendor_selection = t('settingsPanel.models.validation.vendorSelectionRequired');
    } else if (!custom && !preset) {
      next.vendor_selection = t('settingsPanel.models.validation.vendorSelectionInvalid');
    }
    if (!values.protocol.trim()) next.protocol = t('settingsPanel.validation.required');
    if (showApiBase) {
      if (!baseValue) next.api_base = t('config.modelList.apiBaseRequired');
      else if (baseValue.length > 512) next.api_base = t('config.modelList.apiBaseTooLong');
      else if (!/^https?:\/\//i.test(baseValue)) next.api_base = t('config.modelList.apiBaseUrlInvalid');
      else if (values.protocol.trim() === MODELARK_PROTOCOL && !isArkGenerationApiBase(baseValue)) {
        next.api_base = t('settingsPanel.agent.arkGenerationBaseInvalid');
      }
    }
    const apiKey = values.api_key.trim();
    if (!apiKey && !optionalCredentials) next.api_key = t('config.modelList.apiKeyRequired');
    else if (apiKey.length > 2048) next.api_key = t('settingsPanel.models.apiKeyTooLong');
    const modelName = values.model_name.trim();
    if (!modelName && !optionalCredentials) next.model_name = t('config.modelList.modelNameRequired');
    else if (modelName.length > 100) next.model_name = t('config.modelList.modelNameTooLong');
    return next;
  }, [custom, optionalCredentials, preset, showApiBase, t, values]);

  const formItems: FormItem<GenerationDraft>[] = [
    {
      name: 'vendor_selection',
      label: t('settingsPanel.models.vendor'),
      component: 'custom',
      required: true,
      render: ({ id, value, error, disabled: fieldDisabled, onBlur }) => (
        <ModelProviderSelect
          id={id}
          value={String(value ?? '')}
          protocol="openai"
          catalog={pickerCatalog}
          includeOpenAIAccount={false}
          groups={['custom_api', 'other']}
          disabled={fieldDisabled}
          invalid={Boolean(error)}
          onChange={updateVendor}
          onBlur={onBlur}
        />
      ),
    },
    {
      name: 'protocol',
      label: t('settingsPanel.models.protocol'),
      component: 'select',
      required: true,
      options: protocolOptions,
      onChange: (protocol) => {
        // 协议就是模型所属的家族：换了协议，当前模型如果不属于这个家族，就换成这个家族的第一个模型。
        const current = form.getValues().model_name.trim();
        if (catalogApplies && modelProtocol(slot, vendor, catalogAll, current) !== protocol) {
          form.setFieldValue('model_name', generationModelOptions(slot, vendor, catalogAll, protocol, '')[0] ?? '');
        }
      },
    },
  ];

  if (showApiBase) {
    formItems.push({
      name: 'api_base',
      label: t('settingsPanel.fields.api_base.title'),
      component: 'input',
      required: true,
      placeholder: t('settingsPanel.fields.api_base.placeholder'),
      helpTips: isEditableBaseVendor(preset?.vendor_key) ? t(regionalHintKey(preset?.vendor_key)) : undefined,
    });
  }

  formItems.push(
    {
      name: 'api_key',
      label: t('settingsPanel.models.apiKeyLabel'),
      component: 'input',
      type: 'password',
      required: !optionalCredentials,
      passwordVisibilityLabels: {
        show: t('settingsPanel.common.showValue'),
        hide: t('settingsPanel.common.hideValue'),
      },
      placeholder: t('settingsPanel.fields.api_key.placeholder'),
    },
    {
      name: 'model_name',
      label: t('settingsPanel.models.model'),
      component: 'custom',
      required: !optionalCredentials,
      render: ({ id, value, error, disabled: fieldDisabled, onChange, onBlur }) => (
        <ModelNameField
          id={id}
          value={String(value ?? '')}
          mode={custom ? 'manual' : 'options'}
          allowCustomValue
          options={modelOptions}
          disabled={fieldDisabled || !values.vendor_selection}
          invalid={Boolean(error)}
          fetchStatus={arkSelection && fetchStatus?.key === fetchKey ? fetchStatus.text : ''}
          fetching={arkSelection && fetchingKey === fetchKey}
          showRefresh={arkSelection}
          fetchDisabled={!isConnected || !values.api_key.trim() || fetchingKey === fetchKey}
          emptyText={t('settingsPanel.models.noModelResults')}
          onOpen={() => {
            if (arkSelection && attemptedFetchKey !== fetchKey && fetchingKey !== fetchKey) void fetchGenerationModels();
          }}
          onFetch={() => { void fetchGenerationModels(); }}
          onChange={(model) => {
            onChange(model);
            // 选了别的家族的模型，协议跟着切过去（协议下拉里会带上这个值）。
            const family = modelProtocol(slot, vendor, catalogAll, model);
            if (family && family !== form.getValues().protocol) form.setFieldValue('protocol', family);
          }}
          onBlur={onBlur}
        />
      ),
    },
  );

  const confirm = async () => {
    if (Object.keys(errors).length) {
      form.validate();
      return;
    }
    const result = form.validate();
    if (!result.valid) return;
    setSubmitting(true);
    setSaveError('');
    try {
      await save(buildUpdates(slot, result.values, catalog), titleKey);
      onClose();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  return (
    <>
      <FormDialog
        open
        title={t(titleKey)}
        submitting={closeBlocked}
        confirmDisabled={!isConnected}
        confirmLabel={t(enableOnSave ? 'settingsPanel.agent.saveAndEnable' : 'common.save')}
        cancelLabel={t('common.cancel')}
        dialogClassName="settings-model-dialog"
        testIdPrefix="settings-agent-config-dialog"
        testVariant={titleKey}
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        {catalogLoading ? (
          <div className="settings-model-dialog__catalog-status" role="status" aria-live="polite">
            {t('settingsPanel.models.catalogLoadingCustomAvailable')}
          </div>
        ) : null}
        {catalogError ? (
          <div className="settings-model-dialog__catalog-status settings-model-dialog__catalog-status--error" role="alert">
            <span>
              {t('settingsPanel.models.catalogLoadFailedCustomAvailable')}
              <small>{catalogError}</small>
            </span>
            <Button size="sm" disabled={!isConnected || catalogLoading || submitting} onClick={() => void loadCatalog()}>
              {t('settingsPanel.feedback.retry')}
            </Button>
          </div>
        ) : null}
        <Form<GenerationDraft>
          form={form}
          disabled={submitting}
          optionalText={t('common.optional')}
          showOptional={false}
          testIdPrefix="settings-agent-config-dialog"
          rules={{
            vendor_selection: [{ validator: () => errors.vendor_selection }],
            protocol: [{ validator: () => errors.protocol }],
            api_base: [{ validator: () => errors.api_base }],
            api_key: [{ validator: () => errors.api_key }],
            model_name: [{ validator: () => errors.model_name }],
          }}
          items={formItems}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-agent-config-dialog-error">
            {saveError}
          </div>
        ) : null}
      </FormDialog>
      <SettingsConfirmDialog
        open={discardConfirmationOpen}
        title={t('settingsPanel.dialog.discardTitle')}
        message={t('settingsPanel.dialog.discardConfirm')}
        onConfirm={confirmDiscard}
        onCancel={cancelDiscard}
      />
    </>
  );
}
