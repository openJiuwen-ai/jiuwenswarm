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
  normalizeModelOptions,
  parseVendorCatalog,
  selectProviderDefaultModel,
} from '../models/modelAdapters';
import type { MediaCapabilityModality } from './mediaCapabilities';
import {
  buildMediaModelConfigUpdates,
  createMediaModelDraft,
  type MediaModelDraft,
} from './mediaModelConfig';

const EMPTY_VENDOR_CATALOG: VendorPresetMap = {
  reasoning: null,
  token_plan: [],
  coding_plan: [],
  custom_api: [],
};

const FETCH_REASON_KEYS: Record<string, string> = {
  'no remote models endpoint': 'noEndpoint',
  'api_key required for fetch': 'apiKeyRequired',
  'remote fetch failed or empty': 'remoteFailed',
};

type SaveConfig = (updates: Record<string, string>, operation: string) => Promise<unknown>;

function getPresetStatusKey(
  preset: VendorPreset | undefined,
  apiKey: string,
  options: readonly string[],
): string | undefined {
  if (!preset) return undefined;
  if (!preset.models_endpoint)
    return options.length > 0
      ? 'settingsPanel.models.fetchReasons.noEndpoint'
      : 'settingsPanel.models.noPresetModelsNoEndpoint';
  if (preset.models_needs_key && !apiKey.trim())
    return options.length > 0
      ? 'settingsPanel.models.fetchReasons.apiKeyRequired'
      : 'settingsPanel.models.noPresetModelsApiKeyRequired';
  return options.length > 0 ? 'settingsPanel.models.presetModels' : 'settingsPanel.models.noPresetModels';
}

function getModelFetchKey(preset: VendorPreset, apiKey: string): string {
  return JSON.stringify([preset.plan, preset.vendor_key, apiKey.trim()]);
}

function validateMediaModelDraft(
  value: MediaModelDraft,
  catalog: VendorPresetMap,
  t: (key: string, values?: Record<string, unknown>) => string,
): Partial<Record<keyof MediaModelDraft, string>> {
  const errors: Partial<Record<keyof MediaModelDraft, string>> = {};
  const apiBase = value.api_base.trim();
  const apiKey = value.api_key.trim();
  const modelName = value.model_name.trim();

  if (!value.vendor_selection) {
    errors.vendor_selection = t('settingsPanel.models.validation.vendorSelectionRequired');
  } else if (
    value.vendor_selection !== CUSTOM_VENDOR_SELECTION &&
    !findVendorPreset(catalog, value.vendor_selection)
  ) {
    errors.vendor_selection = t('settingsPanel.models.validation.vendorSelectionInvalid');
  }

  if (!apiBase) errors.api_base = t('config.modelList.apiBaseRequired');
  else if (apiBase.length > 512) errors.api_base = t('config.modelList.apiBaseTooLong');
  else if (!/^https?:\/\//i.test(apiBase)) errors.api_base = t('config.modelList.apiBaseUrlInvalid');

  if (!apiKey) errors.api_key = t('config.modelList.apiKeyRequired');
  else if (apiKey.length > 2048) errors.api_key = t('settingsPanel.models.apiKeyTooLong');

  if (!modelName) errors.model_name = t('config.modelList.modelNameRequired');
  else if (modelName.length > 100) errors.model_name = t('config.modelList.modelNameTooLong');

  return errors;
}

export function MediaModelConfigDialog({
  modality,
  config,
  enableOnSave,
  save,
  onSaved,
  onClose,
}: {
  modality: MediaCapabilityModality;
  config: Record<string, unknown>;
  enableOnSave: boolean;
  save: SaveConfig;
  onSaved: (result: unknown) => void;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const initialValues = useMemo(() => createMediaModelDraft(config, modality), [config, modality]);
  const form = useForm<MediaModelDraft>({ initialValues });
  useFormState(form);
  const values = form.getValues();
  const [catalog, setCatalog] = useState<VendorPresetMap>(EMPTY_VENDOR_CATALOG);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [catalogError, setCatalogError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>([]);
  const [fetchStatus, setFetchStatus] = useState('');
  const [saveError, setSaveError] = useState('');
  const catalogRequestId = useRef(0);
  const fetchRequestId = useRef(0);
  const fetchedModelLists = useRef(new Set<string>());
  const preset = findVendorPreset(catalog, values.vendor_selection);
  const custom = values.vendor_selection === CUSTOM_VENDOR_SELECTION;
  const closeBlocked = submitting;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: `agent-${modality}-model-dialog`,
    form,
    closeBlocked,
    onClose,
  });

  const describePresetStatus = useCallback(
    (targetPreset: VendorPreset | undefined, apiKey: string, options: readonly string[]) => {
      const statusKey = getPresetStatusKey(targetPreset, apiKey, options);
      return statusKey ? t(statusKey) : '';
    },
    [t],
  );

  const updateModelOptions = (options: readonly string[]) => {
    const normalized = normalizeModelOptions(options);
    setModelOptions(normalized);
    return normalized;
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
      fetchRequestId.current += 1;
    };
  }, [loadCatalog]);

  useEffect(() => {
    const current = form.getValues();
    const currentPreset = findVendorPreset(catalog, current.vendor_selection);
    if (!currentPreset) return;
    const presetOptions = normalizeModelOptions(currentPreset.model_options);
    const nextOptions =
      current.model_name.trim() && !presetOptions.includes(current.model_name.trim())
        ? [current.model_name.trim(), ...presetOptions]
        : presetOptions;
    setModelOptions(nextOptions);
    setFetchStatus(describePresetStatus(currentPreset, current.api_key, nextOptions));
  }, [catalog, describePresetStatus, form]);

  const invalidateFetchState = () => {
    fetchRequestId.current += 1;
    setFetching(false);
    setFetchStatus('');
  };

  const updateVendor = (selection: string) => {
    invalidateFetchState();
    fetchedModelLists.current.clear();
    if (selection === CUSTOM_VENDOR_SELECTION) {
      form.setValues({
        vendor_selection: selection,
        api_base: '',
        api_key: '',
        model_name: '',
        model_input_mode: 'manual',
        provider: 'OpenAI',
        endpoint_profile: '',
        vendor_key: '',
        plan: '',
      });
      setModelOptions([]);
      form.clearValidate(['vendor_selection', 'api_base', 'api_key', 'model_name']);
      return;
    }

    const nextPreset = findVendorPreset(catalog, selection);
    if (!nextPreset) {
      form.setFieldValue('vendor_selection', selection);
      return;
    }
    const nextOptions = normalizeModelOptions(nextPreset.model_options);
    form.setValues({
      vendor_selection: selection,
      api_base: nextPreset.api_base,
      api_key: '',
      model_name: selectProviderDefaultModel(nextPreset.default_model, nextOptions),
      model_input_mode: 'options',
      provider: nextPreset.client_provider,
      endpoint_profile: nextPreset.endpoint_profile ?? '',
      vendor_key: nextPreset.vendor_key,
      plan: nextPreset.plan,
    });
    form.clearValidate(['vendor_selection', 'api_base', 'api_key', 'model_name']);
    setModelOptions(nextOptions);
    setFetchStatus(describePresetStatus(nextPreset, '', nextOptions));
  };

  const fetchModels = async () => {
    const currentValues = form.getValues();
    const currentPreset = findVendorPreset(catalog, currentValues.vendor_selection);
    if (!currentPreset || fetching) return;
    const presetOptions = normalizeModelOptions(currentPreset.model_options);
    if (!currentPreset.models_endpoint || (currentPreset.models_needs_key && !currentValues.api_key.trim())) {
      updateModelOptions(presetOptions);
      setFetchStatus(describePresetStatus(currentPreset, currentValues.api_key, presetOptions));
      return;
    }
    const currentRequestId = ++fetchRequestId.current;
    setFetching(true);
    setFetchStatus(t('settingsPanel.models.fetchModelsLoading'));
    try {
      const result = await request<VendorFetchModelsResult>(
        'vendors.fetch_models',
        {
          vendor_key: currentPreset.vendor_key,
          plan: currentPreset.plan,
          api_key: currentValues.api_key.trim(),
        },
        { timeoutMs: 30_000 },
      );
      if (currentRequestId !== fetchRequestId.current) return;
      if (!Array.isArray(result.models) || !result.models.every((name) => typeof name === 'string')) {
        throw new Error(t('settingsPanel.models.fetchModelsInvalidResponse'));
      }
      const nextOptions = normalizeModelOptions(result.models);
      if (result.source === 'remote') {
        if (nextOptions.length === 0) throw new Error(t('settingsPanel.models.fetchModelsInvalidResponse'));
        updateModelOptions(nextOptions);
        fetchedModelLists.current.add(getModelFetchKey(currentPreset, currentValues.api_key));
        setFetchStatus(t('settingsPanel.models.fetchModelsRemote', { count: nextOptions.length }));
      } else if (result.source === 'preset' && result.reason && FETCH_REASON_KEYS[result.reason]) {
        updateModelOptions(nextOptions);
        setFetchStatus(t(`settingsPanel.models.fetchReasons.${FETCH_REASON_KEYS[result.reason]}`));
      } else {
        throw new Error(t('settingsPanel.models.fetchModelsUnrecognizedResult'));
      }
    } catch (error) {
      if (currentRequestId === fetchRequestId.current) {
        updateModelOptions(presetOptions);
        const message = error instanceof Error ? error.message : t('settingsPanel.models.fetchModelsFailed');
        setFetchStatus(
          presetOptions.length > 0
            ? t('settingsPanel.models.fetchModelsFailedUsingPreset', { error: message })
            : message,
        );
      }
    } finally {
      if (currentRequestId === fetchRequestId.current) setFetching(false);
    }
  };

  const openModelList = () => {
    const currentValues = form.getValues();
    const currentPreset = findVendorPreset(catalog, currentValues.vendor_selection);
    if (!currentPreset) return;
    const presetOptions = normalizeModelOptions(currentPreset.model_options);
    if (!currentPreset.models_endpoint || (currentPreset.models_needs_key && !currentValues.api_key.trim())) {
      updateModelOptions(presetOptions);
      setFetchStatus(describePresetStatus(currentPreset, currentValues.api_key, presetOptions));
      return;
    }
    const fetchKey = getModelFetchKey(currentPreset, currentValues.api_key);
    if (!fetchedModelLists.current.has(fetchKey)) void fetchModels();
  };

  const errors = validateMediaModelDraft(values, catalog, t);
  const formItems: FormItem<MediaModelDraft>[] = [
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
      options: [{ value: 'openai', label: t('settingsPanel.models.protocols.openai') }],
    },
  ];

  if (custom) {
    formItems.push({
      name: 'api_base',
      label: t('settingsPanel.fields.api_base.title'),
      component: 'input',
      required: true,
      placeholder: t('settingsPanel.fields.api_base.placeholder'),
    });
  }

  formItems.push({
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
    onChange: (_apiKey, nextValues) => {
      invalidateFetchState();
      fetchedModelLists.current.clear();
      const nextPreset = findVendorPreset(catalog, nextValues.vendor_selection);
      if (!nextPreset) return;
      const nextOptions = updateModelOptions(nextPreset.model_options);
      setFetchStatus(describePresetStatus(nextPreset, nextValues.api_key, nextOptions));
    },
  });

  formItems.push({
    name: 'model_name',
    label: t('settingsPanel.models.model'),
    component: 'custom',
    required: true,
    render: ({ id, value, error, disabled: fieldDisabled, onChange, onBlur }) => (
      <ModelNameField
        id={id}
        value={String(value ?? '')}
        mode={values.model_input_mode}
        options={modelOptions}
        disabled={fieldDisabled || !values.vendor_selection}
        invalid={Boolean(error)}
        fetchStatus={fetchStatus}
        fetching={fetching}
        showRefresh={Boolean(preset)}
        fetchDisabled={!isConnected || !preset?.models_endpoint || fetching || submitting}
        emptyText={t(
          preset
            ? (getPresetStatusKey(preset, values.api_key, []) ?? 'settingsPanel.models.noModelResults')
            : 'settingsPanel.models.noModelResults',
        )}
        onOpen={openModelList}
        onFetch={() => void fetchModels()}
        onChange={onChange}
        onBlur={onBlur}
      />
    ),
  });

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
      const saveResult = await save(
        buildMediaModelConfigUpdates(result.values, catalog, modality, enableOnSave),
        `settingsPanel.agent.${modality}`,
      );
      onSaved(saveResult);
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
        title={t(`settingsPanel.agent.${modality}ConfigTitle`)}
        submitting={closeBlocked}
        confirmDisabled={!isConnected || fetching}
        confirmLabel={t(enableOnSave ? 'settingsPanel.agent.saveAndEnable' : 'common.save')}
        cancelLabel={t('common.cancel')}
        dialogClassName="settings-model-dialog"
        testIdPrefix="settings-agent-media-config-dialog"
        testVariant={modality}
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        {catalogLoading ? (
          <div className="settings-model-dialog__catalog-status" role="status" aria-live="polite" data-testid="settings-agent-media-config-dialog-catalog-loading">
            {t('settingsPanel.models.catalogLoadingCustomAvailable')}
          </div>
        ) : null}
        {catalogError ? (
          <div
            className="settings-model-dialog__catalog-status settings-model-dialog__catalog-status--error"
            role="alert"
            data-testid="settings-agent-media-config-dialog-catalog-error"
          >
            <span>
              {t('settingsPanel.models.catalogLoadFailedCustomAvailable')}
              <small>{catalogError}</small>
            </span>
            <Button size="sm" disabled={!isConnected || catalogLoading || submitting} onClick={() => void loadCatalog()} data-testid="settings-agent-media-config-dialog-catalog-retry-btn">
              {t('settingsPanel.feedback.retry')}
            </Button>
          </div>
        ) : null}
        <Form<MediaModelDraft>
          form={form}
          disabled={submitting}
          optionalText={t('common.optional')}
          showOptional={false}
          testIdPrefix="settings-agent-media-config-dialog"
          rules={{
            vendor_selection: [{ validator: () => errors.vendor_selection }],
            api_base: [{ validator: () => errors.api_base }],
            api_key: [{ validator: () => errors.api_key }],
            model_name: [{ validator: () => errors.model_name }],
          }}
          items={formItems}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-agent-media-config-dialog-error">
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
