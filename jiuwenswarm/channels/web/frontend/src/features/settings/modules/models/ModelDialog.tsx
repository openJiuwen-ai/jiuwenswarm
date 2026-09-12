import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, VendorFetchModelsResult, VendorPreset, VendorPresetMap } from '../../../../types';
import { Button } from '../../../../components/ui';
import { Form, FormDialog, useForm, type FormItem } from '../../../../components/form';
import { buildModelValidationPayload } from '../../services/settingsContract';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { OpenAIAccountSettings, useOpenAIAccountController } from './OpenAIAccountField';
import { ModelNameField } from './ModelNameField';
import { ModelProviderSelect } from './ModelProviderSelect';
import {
  CUSTOM_VENDOR_SELECTION,
  OPENAI_ACCOUNT_SELECTION,
  applyModelProtocol,
  applyVendorSelection,
  createModelDraft,
  findVendorPreset,
  modelDraftToEntry,
  normalizeModelOptions,
  rebaseModelDraft,
  reconcileModelReasoning,
  selectProviderDefaultModel,
  type ModelDraft,
  type ModelProtocol,
} from './modelAdapters';
import { validateModelDraft } from './modelValidation';
import { buildReasoningOptions, resolveModelReasoning } from './modelReasoning';

type ConnectionFailure = {
  error: string;
  snapshot: ModelEntry;
};

const FETCH_REASON_KEYS: Record<string, string> = {
  'no remote models endpoint': 'noEndpoint',
  'api_key required for fetch': 'apiKeyRequired',
  'remote fetch failed or empty': 'remoteFailed',
  'remote returned HTTP 401': 'remoteAuthFailed',
  'remote returned HTTP 403': 'remoteAuthFailed',
  'remote returned HTTP 429': 'remoteRateLimited',
};

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

export function ModelDialog({
  model,
  models,
  catalog,
  catalogLoading,
  catalogError,
  saving,
  onClose,
  onSave,
  onRetryCatalog,
}: {
  model?: ModelEntry;
  models: ModelEntry[];
  catalog: VendorPresetMap;
  catalogLoading: boolean;
  catalogError: string;
  saving: boolean;
  onClose: () => void;
  onSave: (model: ModelEntry) => Promise<void>;
  onRetryCatalog: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const initialValues = useMemo(() => createModelDraft(model, catalog), [catalog, model]);
  const form = useForm({ initialValues });
  const [submitting, setSubmitting] = useState(false);
  const [testing, setTesting] = useState(false);
  const [fetching, setFetching] = useState(false);
  const [modelOptions, setModelOptions] = useState<string[]>(() => {
    const preset = findVendorPreset(catalog, initialValues.vendor_selection);
    const options = normalizeModelOptions(preset?.model_options ?? []);
    const currentModel = initialValues.model_name.trim();
    return preset && currentModel && !options.includes(currentModel) ? [currentModel, ...options] : options;
  });
  const [fetchStatus, setFetchStatus] = useState(() => {
    const preset = findVendorPreset(catalog, initialValues.vendor_selection);
    const options = normalizeModelOptions(preset?.model_options ?? []);
    const statusKey = getPresetStatusKey(preset, initialValues.api_key, options);
    return statusKey ? t(statusKey) : '';
  });
  const [validationFailure, setValidationFailure] = useState<ConnectionFailure | null>(null);
  const [saveError, setSaveError] = useState('');
  const [logoutConfirmation, setLogoutConfirmation] = useState(false);
  const catalogBaseline = useRef(initialValues);
  const connectionChanged = useRef(false);
  const validationRequestId = useRef(0);
  const fetchRequestId = useRef(0);
  const fetchedModelLists = useRef(new Set<string>());
  const closeBlocked = submitting || saving;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'model-dialog',
    form,
    closeBlocked,
    onClose,
  });
  const values = form.getValues();
  const errors = validateModelDraft(values, models, model?.origin_index, catalog, t);
  const account = values.vendor_selection === OPENAI_ACCOUNT_SELECTION;
  const preset = findVendorPreset(catalog, values.vendor_selection);
  const custom = values.vendor_selection === CUSTOM_VENDOR_SELECTION;
  const reasoning = values.vendor_selection
    ? resolveModelReasoning(catalog, preset, values.model_name, values.protocol)
    : null;
  const applyModelDraft = useCallback(
    (draft: ModelDraft): void => {
      form.setValues(reconcileModelReasoning(draft, catalog));
      form.clearValidate('reasoning_level');
    },
    [catalog, form],
  );
  const openAIAccount = useOpenAIAccountController({
    active: account,
    model: modelDraftToEntry(values, model, catalog, connectionChanged.current),
    connected: isConnected,
    request,
    onModelPatch: (patch) => {
      const current = form.getValues();
      const next = {
        ...current,
        ...(patch.model_name !== undefined ? { model_name: patch.model_name } : {}),
        ...(patch.api_base !== undefined ? { api_base: patch.api_base } : {}),
        ...(patch.api_key !== undefined ? { api_key: patch.api_key } : {}),
      };
      if (next.model_name !== current.model_name) {
        invalidateConnectionState();
      }
      applyModelDraft(next);
    },
  });

  const describePresetStatus = (targetPreset: VendorPreset | undefined, apiKey: string, options: readonly string[]) => {
    const statusKey = getPresetStatusKey(targetPreset, apiKey, options);
    return statusKey ? t(statusKey) : '';
  };

  const updateModelOptions = (options: readonly string[]) => {
    const normalizedOptions = normalizeModelOptions(options);
    setModelOptions(normalizedOptions);
    return normalizedOptions;
  };

  useEffect(
    () => () => {
      validationRequestId.current += 1;
      fetchRequestId.current += 1;
    },
    [],
  );

  useEffect(() => {
    if (!catalog.reasoning) return;
    const current = form.getValues();
    const next = createModelDraft(model, catalog);
    if (
      !model?.vendor_key ||
      connectionChanged.current ||
      !next.vendor_selection ||
      next.vendor_selection === CUSTOM_VENDOR_SELECTION ||
      current.vendor_selection === next.vendor_selection
    ) {
      applyModelDraft(current);
      return;
    }
    const rebasedValues = rebaseModelDraft(current, catalogBaseline.current, next);
    form.reset(next);
    applyModelDraft(rebasedValues);
    catalogBaseline.current = next;
    const nextPreset = findVendorPreset(catalog, next.vendor_selection);
    const presetOptions = normalizeModelOptions(nextPreset?.model_options ?? []);
    const nextOptions =
      next.model_name && !presetOptions.includes(next.model_name) ? [next.model_name, ...presetOptions] : presetOptions;
    setModelOptions(nextOptions);
    setFetchStatus(describePresetStatus(nextPreset, next.api_key, nextOptions));
  }, [applyModelDraft, catalog, form, model, t]);

  const invalidateConnectionState = () => {
    validationRequestId.current += 1;
    fetchRequestId.current += 1;
    setTesting(false);
    setFetching(false);
    setValidationFailure(null);
    setFetchStatus('');
  };

  const updateProtocol = (protocol: ModelProtocol) => {
    connectionChanged.current = true;
    invalidateConnectionState();
    const next = applyModelProtocol(form.getValues(), protocol, catalog);
    applyModelDraft(next);
    setFetchStatus(describePresetStatus(findVendorPreset(catalog, next.vendor_selection), next.api_key, modelOptions));
  };

  const updateVendor = (selection: string) => {
    connectionChanged.current = true;
    invalidateConnectionState();
    fetchedModelLists.current.clear();
    const next = applyVendorSelection(form.getValues(), selection, catalog);
    const nextPreset = findVendorPreset(catalog, selection);
    const nextOptions = normalizeModelOptions(nextPreset?.model_options ?? []);
    const nextModel = selectProviderDefaultModel(next.model_name, nextOptions);
    applyModelDraft({ ...next, model_name: nextModel });
    form.clearValidate(['api_key', 'model_name']);
    setModelOptions(nextOptions);
    setFetchStatus(describePresetStatus(nextPreset, next.api_key, nextOptions));
  };

  const buildEntry = () => modelDraftToEntry(form.getValues(), model, catalog, connectionChanged.current);

  const persist = async (snapshot: ModelEntry) => {
    setSubmitting(true);
    setSaveError('');
    try {
      await onSave(snapshot);
      onClose();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSubmitting(false);
    }
  };

  const validateAndSave = async () => {
    if (Object.keys(errors).length) {
      form.validate();
      return;
    }
    const currentRequestId = ++validationRequestId.current;
    const snapshot = buildEntry();
    setTesting(true);
    setSaveError('');
    setValidationFailure(null);
    try {
      await request('config.validate_model', buildModelValidationPayload(snapshot), { timeoutMs: 60_000 });
      if (currentRequestId === validationRequestId.current) {
        setTesting(false);
        await persist(snapshot);
      }
    } catch (error) {
      if (currentRequestId === validationRequestId.current) {
        const detail = error instanceof Error ? error.message.trim() : '';
        setValidationFailure({
          error: detail
            ? t('settingsPanel.models.validationFailedWithDetail', { detail })
            : t('settingsPanel.models.validationFailed'),
          snapshot,
        });
      }
    } finally {
      if (currentRequestId === validationRequestId.current) setTesting(false);
    }
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
      } else if (result.source === 'preset' && result.reason) {
        updateModelOptions(nextOptions);
        const reasonKey = FETCH_REASON_KEYS[result.reason];
        setFetchStatus(
          reasonKey
            ? t(`settingsPanel.models.fetchReasons.${reasonKey}`)
            : result.reason,
        );
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
    if (fetchedModelLists.current.has(fetchKey)) return;
    void fetchModels();
  };

  const anthropicUnavailable = Boolean(preset && (!preset.supports_anthropic || !preset.anthropic_base));
  const protocolOptions = [
    { value: 'openai', label: t('settingsPanel.models.protocols.openai') },
    {
      value: 'anthropic',
      label: t('settingsPanel.models.protocols.anthropic'),
      disabled: anthropicUnavailable,
      disabledReason: anthropicUnavailable ? t('settingsPanel.models.protocols.anthropicUnavailable') : undefined,
    },
  ];

  const formItems: FormItem<ModelDraft>[] = [];
  formItems.push({
    name: 'vendor_selection',
    label: t('settingsPanel.models.vendor'),
    component: 'custom',
    required: true,
    render: ({ id, value, error, disabled, onBlur }) => (
      <div className="settings-model-provider-field">
        <ModelProviderSelect
          id={id}
          value={String(value ?? '')}
          protocol={values.protocol}
          catalog={catalog}
          disabled={disabled}
          invalid={Boolean(error)}
          onChange={updateVendor}
          onBlur={onBlur}
        />
        {account ? (
          <OpenAIAccountSettings
            controller={openAIAccount}
            connected={isConnected}
            disabled={testing || submitting || saving}
            onRequestLogout={() => setLogoutConfirmation(true)}
          />
        ) : null}
      </div>
    ),
  });
  if (!account) {
    formItems.push({
      name: 'protocol',
      label: t('settingsPanel.models.protocol'),
      component: 'select',
      required: true,
      options: protocolOptions,
      onChange: (value) => updateProtocol(value as ModelProtocol),
    });
    if (custom) {
      formItems.push({
        name: 'api_base',
        label: t('settingsPanel.fields.api_base.title'),
        component: 'input',
        required: true,
        placeholder: t('settingsPanel.fields.api_base.placeholder'),
        onChange: () => {
          connectionChanged.current = true;
          invalidateConnectionState();
        },
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
        invalidateConnectionState();
        fetchedModelLists.current.clear();
        const nextPreset = findVendorPreset(catalog, nextValues.vendor_selection);
        if (!nextPreset) return;
        const nextOptions = updateModelOptions(nextPreset.model_options);
        setFetchStatus(describePresetStatus(nextPreset, nextValues.api_key, nextOptions));
      },
    });
  }
  formItems.push({
    name: 'model_name',
    label: t('settingsPanel.models.model'),
    component: 'custom',
    required: true,
    helpTips: t('settingsPanel.models.modelIdHint'),
    onChange: (_modelName, nextValues) => {
      invalidateConnectionState();
      applyModelDraft(nextValues);
    },
    render: ({ id, value, error, disabled, onChange, onBlur }) => (
      <ModelNameField
        id={id}
        value={String(value ?? '')}
        mode={values.model_input_mode}
        options={account ? openAIAccount.modelOptions : modelOptions}
        disabled={disabled || (account ? !openAIAccount.authenticated : !model && !values.vendor_selection)}
        invalid={Boolean(error)}
        fetchStatus={account ? openAIAccount.modelStatus : fetchStatus}
        fetchStatusTone={account ? openAIAccount.modelStatusTone : 'neutral'}
        fetching={account ? openAIAccount.loadingModels : fetching}
        showRefresh={account || Boolean(preset)}
        fetchDisabled={
          account
            ? !isConnected ||
              !openAIAccount.authenticated ||
              testing ||
              openAIAccount.loadingModels ||
              submitting ||
              saving
            : !isConnected || !preset?.models_endpoint || testing || fetching || submitting || saving
        }
        emptyText={
          account
            ? t(
                openAIAccount.authenticated
                  ? 'config.openaiAccount.noModelsAvailable'
                  : 'config.openaiAccount.needLoginForModel',
              )
            : preset
              ? t(getPresetStatusKey(preset, values.api_key, []) ?? 'settingsPanel.models.noModelResults')
              : t('settingsPanel.models.noModelResults')
        }
        onOpen={account ? () => undefined : openModelList}
        onFetch={() => void (account ? openAIAccount.refreshModels() : fetchModels())}
        onChange={onChange}
        onBlur={onBlur}
      />
    ),
  });
  formItems.push({
    name: 'alias',
    label: t('settingsPanel.models.customName'),
    component: 'input',
    placeholder: t('settingsPanel.models.customNamePlaceholder'),
  });
  if (reasoning && reasoning.options.length > 0) {
    formItems.push({
      name: 'reasoning_level',
      label: t('settingsPanel.fields.reasoning_level.title'),
      component: 'select',
      options: buildReasoningOptions(reasoning, t('settingsPanel.models.reasoning.auto'), (value) =>
        t(`settingsPanel.models.reasoning.options.${value}`, { defaultValue: value }),
      ),
      onChange: invalidateConnectionState,
    });
  }

  return (
    <>
      <FormDialog
        open
        title={t(model ? 'settingsPanel.models.editModel' : 'settingsPanel.models.addModel')}
        submitting={closeBlocked}
        confirmLoading={testing}
        confirmDisabled={
          !isConnected ||
          saving ||
          testing ||
          fetching ||
          catalogLoading ||
          Boolean(catalogError) ||
          !catalog.reasoning ||
          (account && openAIAccount.busy)
        }
        confirmLabel={t(
          testing
            ? 'settingsPanel.models.testingConnection'
            : submitting || saving
              ? 'settingsPanel.models.savingModel'
              : 'common.confirm',
        )}
        cancelLabel={t('common.cancel')}
        dialogClassName="settings-model-dialog"
        testIdPrefix="settings-model-dialog"
        onConfirm={() => void validateAndSave()}
        onCancel={requestClose}
      >
        {catalogLoading ? (
          <div className="settings-model-dialog__catalog-status" role="status" aria-live="polite" data-testid="settings-model-dialog-catalog-loading">
            {t('settingsPanel.models.catalogLoading')}
          </div>
        ) : null}
        {catalogError ? (
          <div
            className="settings-model-dialog__catalog-status settings-model-dialog__catalog-status--error"
            role="alert"
            data-testid="settings-model-dialog-catalog-error"
          >
            <span>
              {t('settingsPanel.models.catalogLoadFailed')}
              <small>{catalogError}</small>
            </span>
            <Button
              size="sm"
              disabled={!isConnected || catalogLoading || testing || submitting || saving}
              onClick={onRetryCatalog}
              data-testid="settings-model-dialog-catalog-retry-btn"
            >
              {t('settingsPanel.feedback.retry')}
            </Button>
          </div>
        ) : null}
        <Form<ModelDraft>
          form={form}
          disabled={testing || submitting || saving || !catalog.reasoning}
          optionalText={t('common.optional')}
          showOptional={false}
          testIdPrefix="settings-model-dialog"
          rules={{
            alias: [
              {
                trigger: ['change', 'blur'],
                validator: (_alias, currentValues) =>
                  validateModelDraft({ ...currentValues }, models, model?.origin_index, catalog, t).alias,
              },
            ],
            protocol: [{ validator: () => errors.protocol }],
            vendor_selection: [{ validator: () => errors.vendor_selection }],
            model_name: [{ validator: () => errors.model_name }],
            api_key: [{ validator: () => errors.api_key }],
            api_base: [{ validator: () => errors.api_base }],
            reasoning_level: [{ validator: () => errors.reasoning_level }],
          }}
          items={formItems}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-model-dialog-error">
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
      <SettingsConfirmDialog
        open={validationFailure !== null}
        title={t('settingsPanel.models.validationBeforeSaveTitle')}
        message={t('settingsPanel.models.validationBeforeSaveMessage')}
        error={validationFailure?.error}
        confirming={submitting || saving}
        confirmLabel={t('settingsPanel.models.continueSave')}
        cancelLabel={t('settingsPanel.models.returnToEdit')}
        confirmVariant="warning"
        onCancel={() => setValidationFailure(null)}
        onConfirm={() => {
          if (!validationFailure) return;
          const { snapshot } = validationFailure;
          setValidationFailure(null);
          void persist(snapshot);
        }}
      />
      <SettingsConfirmDialog
        open={logoutConfirmation}
        title={t('config.openaiAccount.logoutConfirmTitle')}
        message={t('config.openaiAccount.logoutConfirmMessage')}
        confirming={openAIAccount.loggingOut}
        confirmLabel={t('config.openaiAccount.logoutConfirm')}
        cancelLabel={t('common.cancel')}
        confirmVariant="danger"
        onCancel={() => {
          if (!openAIAccount.loggingOut) setLogoutConfirmation(false);
        }}
        onConfirm={() => {
          void openAIAccount.logout().then(() => setLogoutConfirmation(false));
        }}
      />
    </>
  );
}
