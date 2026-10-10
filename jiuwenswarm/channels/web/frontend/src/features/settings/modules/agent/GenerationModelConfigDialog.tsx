import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { VendorPreset, VendorPresetMap } from '../../../../types';
import { Button } from '../../../../components/ui';
import { Form, FormDialog, useForm, useFormState, type FormItem } from '../../../../components/form';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { ContextWindowField } from '../models/ContextWindowField';
import { ModelNameField } from '../models/ModelNameField';
import { ModelProviderSelect } from '../models/ModelProviderSelect';
import {
  normalizeContextWindowTokens,
  parseContextWindowTokens,
  resolveDraftContextWindowTokens,
} from '../models/contextWindow';
import {
  CUSTOM_VENDOR_SELECTION,
  findVendorPreset,
  flattenVendorCatalog,
  parseVendorCatalog,
  vendorSelectionKey,
} from '../models/modelAdapters';
import {
  catalogProtocols,
  defaultGeneration,
  generationModelOptions,
  generationProviderName,
  generationVendor,
  isRegionalVendor,
  modelProtocol,
  regionalHintKey,
  withCurrentOption,
  type GenerationSlot,
} from './generationModels';

const EMPTY_VENDOR_CATALOG: VendorPresetMap = {
  reasoning: null,
  token_plan: [],
  coding_plan: [],
  custom_api: [],
};

const OPENAI_PROTOCOL = 'openai';

type SaveConfig = (updates: Record<string, string>, operation: string) => Promise<unknown>;

type GenerationDraft = {
  vendor_selection: string;
  protocol: string;
  api_base: string;
  api_key: string;
  model_name: string;
  context_window_tokens: string;
};

function readConfig(config: Readonly<Record<string, unknown>>, slot: GenerationSlot, suffix: string): string {
  return String(config[`${slot}_${suffix}`] ?? '');
}

function trimSlash(value: string): string {
  return value.trim().replace(/\/+$/, '').toLowerCase();
}

/** 之前保存下来的配置只有 provider 名称和 api_base（没有厂商 key），按 api_base
 *  优先、其次按名称找回对应的厂商预设，这样编辑已有配置时下拉能回显成对应厂商。 */
function findStoredPreset(catalog: VendorPresetMap, provider: string, apiBase: string): VendorPreset | undefined {
  const presets = flattenVendorCatalog(catalog);
  const base = trimSlash(apiBase);
  const name = provider.trim().toLowerCase();
  return (
    (base ? presets.find((preset) => trimSlash(preset.api_base) === base) : undefined) ??
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
    context_window_tokens: normalizeContextWindowTokens(readConfig(config, slot, 'context_window_tokens')),
  };
}

function buildUpdates(slot: GenerationSlot, draft: GenerationDraft, catalog: VendorPresetMap): Record<string, string> {
  const preset = findVendorPreset(catalog, draft.vendor_selection);
  const editableBase = !preset || isRegionalVendor(preset.vendor_key);
  return {
    [`${slot}_provider`]: generationProviderName(preset?.vendor_key, preset?.client_provider),
    [`${slot}_protocol`]: draft.protocol.trim(),
    [`${slot}_api_base`]: (editableBase ? draft.api_base : preset.api_base).trim(),
    [`${slot}_api_key`]: draft.api_key.trim(),
    [`${slot}_model`]: draft.model_name.trim(),
    [`${slot}_context_window_tokens`]: String(resolveDraftContextWindowTokens(draft.context_window_tokens)),
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
  const closeBlocked = submitting;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: `agent-${slot}-model-dialog`,
    form,
    closeBlocked,
    onClose,
  });

  const preset = findVendorPreset(catalog, values.vendor_selection);
  const custom = values.vendor_selection === CUSTOM_VENDOR_SELECTION;
  // API 地址：自定义、以及密钥分区域的厂商（MiniMax）让用户填/改；其余厂商用预设地址，不展示。
  const showApiBase = custom || isRegionalVendor(preset?.vendor_key);
  const apiBase = (showApiBase ? values.api_base : (preset?.api_base ?? values.api_base)).trim();
  // 用哪份模型目录：OpenRouter / MiniMax 有各自的目录；自定义地址给出全部作为候选
  // （可能是兼容它们的代理），仍允许手输别的 ID；其他厂商没有已知的生成模型，只能手输。
  const vendor = generationVendor(preset?.vendor_key, custom ? '' : apiBase);
  const catalogAll = custom;
  const catalogApplies = catalogAll || vendor !== undefined;

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
    const match = findStoredPreset(catalog, readConfig(config, slot, 'provider'), current.api_base);
    if (!match) return;
    const next = { ...current, vendor_selection: vendorSelectionKey(match.plan, match.vendor_key) };
    // 没有专属目录的厂商只支持 OpenAI 协议，旧配置里残留的其他协议值一并纠正。
    if (!generationVendor(match.vendor_key, match.api_base)) next.protocol = OPENAI_PROTOCOL;
    if (form.hasUnsavedChanges()) form.setValues({ vendor_selection: next.vendor_selection });
    else form.reset(next);
  }, [catalog, config, form, slot]);

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
      api_base: nextPreset.api_base,
      api_key: '',
      protocol: nextVendor ? defaults.protocol : OPENAI_PROTOCOL,
      model_name: defaults.model,
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
  const modelOptions = catalogApplies
    ? generationModelOptions(slot, vendor, catalogAll, values.protocol, values.model_name)
    : [];

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
    }
    const apiKey = values.api_key.trim();
    if (!apiKey) next.api_key = t('config.modelList.apiKeyRequired');
    else if (apiKey.length > 2048) next.api_key = t('settingsPanel.models.apiKeyTooLong');
    const modelName = values.model_name.trim();
    if (!modelName) next.model_name = t('config.modelList.modelNameRequired');
    else if (modelName.length > 100) next.model_name = t('config.modelList.modelNameTooLong');
    if (parseContextWindowTokens(values.context_window_tokens) === null) {
      next.context_window_tokens = t('settingsPanel.models.validation.contextWindowInvalid');
    }
    return next;
  }, [custom, preset, showApiBase, t, values]);

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
          catalog={catalog}
          includeOpenAIAccount={false}
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
      helpTips: isRegionalVendor(preset?.vendor_key) ? t(regionalHintKey(preset?.vendor_key)) : undefined,
    });
  }

  formItems.push(
    {
      name: 'api_key',
      label: t('settingsPanel.models.apiKeyLabel'),
      component: 'input',
      type: 'password',
      required: true,
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
      required: true,
      render: ({ id, value, error, disabled: fieldDisabled, onChange, onBlur }) => (
        <ModelNameField
          id={id}
          value={String(value ?? '')}
          mode="options"
          allowCustomValue
          options={modelOptions}
          disabled={fieldDisabled || !values.vendor_selection}
          invalid={Boolean(error)}
          fetchStatus=""
          fetching={false}
          showRefresh={false}
          fetchDisabled
          emptyText={t('settingsPanel.models.noModelResults')}
          onOpen={() => undefined}
          onFetch={() => undefined}
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
    {
      name: 'context_window_tokens',
      label: t('settingsPanel.models.contextWindow'),
      component: 'custom',
      required: true,
      helpTips: t('settingsPanel.models.contextWindowHint'),
      render: ({ id, value, error, disabled, onChange, onBlur }) => (
        <ContextWindowField
          id={id}
          value={value}
          error={error}
          disabled={disabled}
          placeholder={t('settingsPanel.models.contextWindowPlaceholder')}
          presetLabel={t('settingsPanel.models.contextWindowPresets')}
          onChange={onChange}
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
            context_window_tokens: [{ validator: () => errors.context_window_tokens }],
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
