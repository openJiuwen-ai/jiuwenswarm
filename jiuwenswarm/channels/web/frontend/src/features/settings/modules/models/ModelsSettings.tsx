import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, ModelGroup, VendorPresetMap } from '../../../../types';
import { Button, Tag } from '../../../../components/ui';
import { settingsActionIcons, settingsEmptyBoxIllustration } from '../../../../assets/settings';
import { getModelLogoUrl } from '../../../../assets/providers';
import { buildModelValidationPayload } from '../../services/settingsContract';
import type { SettingsSaveErrorScope } from '../../services/SettingsSaveQueue';
import {
  SettingsConfirmDialog,
  SettingsSection,
  SettingsTabLayout,
  type SettingsTabDefinition,
} from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { FreeModelsSettings } from './FreeModelsSettings';
import { ModelDialog } from './ModelDialog';
import { ModelGroupDialog } from './ModelGroupDialog';
import { ModelGroupsSettings } from './ModelGroupsSettings';
import { getVendorLabel } from './ModelProviderSelect';
import { displayModelProtocol, parseVendorCatalog, withoutGenerationOnlyPresets } from './modelAdapters';
import {
  getGroupDefaultRouteId,
  getModelGroupDisplayName,
  parseModelGroup,
  parseModelGroups,
  promoteGroupRouteDefault,
  removeGroupRoute,
} from './modelGroupAdapters';
import { useSessionStore } from '../../../../stores/sessionStore';
import { getEditableModels, isRuntimeGrantedModel } from './modelListOperations';

const EMPTY_VENDOR_CATALOG: VendorPresetMap = {
  reasoning: null,
  token_plan: [],
  coding_plan: [],
  custom_api: [],
};

type ModelConfirmation =
  | { type: 'delete-model'; model: ModelEntry; message: string }
  | { type: 'delete-group'; group: ModelGroup; message: string }
  | { type: 'remove-group-route'; group: ModelGroup; routeId: string; message: string };

type ValidationToast = { success: boolean; message: string };
type SaveModelsOptions = { errorScope?: SettingsSaveErrorScope };

type ModelResourceResult = { model?: unknown; applied_without_restart?: unknown };

function isRecord(value: unknown): value is Record<string, unknown> {
  return Boolean(value) && typeof value === 'object' && !Array.isArray(value);
}

export function parseModelResource(payload: unknown): ModelEntry {
  if (!isRecord(payload) || !isRecord(payload.model)) throw new Error('INVALID_MODEL_RESOURCE');
  const model = payload.model;
  const client = isRecord(model.model_client_config) ? model.model_client_config : {};
  const config = isRecord(model.model_config_obj) ? model.model_config_obj : {};
  const writeOnlyFields = Array.isArray(model.write_only_fields) ? model.write_only_fields : [];
  if (typeof client.model_name !== 'string' || typeof client.api_base !== 'string') {
    throw new Error('INVALID_MODEL_RESOURCE');
  }
  return {
    model_id: typeof model.model_id === 'string' ? model.model_id : undefined,
    alias: typeof model.alias === 'string' ? model.alias : '',
    model_name: client.model_name,
    api_base: client.api_base,
    api_key: typeof client.api_key === 'string' ? client.api_key : '',
    has_api_key: Boolean(client.api_key) || writeOnlyFields.includes('model_client_config.api_key'),
    model_provider: typeof client.client_provider === 'string' ? client.client_provider : '',
    timeout: typeof client.timeout === 'number' ? client.timeout : undefined,
    temperature: typeof config.temperature === 'number' ? config.temperature : undefined,
    reasoning_level: typeof config.reasoning_level === 'string' ? config.reasoning_level : '',
    context_window_tokens: typeof config.context_window === 'number' ? config.context_window : undefined,
    is_default: model.is_default === true,
    vendor_key: typeof client.vendor_key === 'string' ? client.vendor_key : undefined,
    plan:
      client.plan === 'token_plan' || client.plan === 'coding_plan' || client.plan === 'custom_api'
        ? client.plan
        : undefined,
    endpoint_profile: typeof client.endpoint_profile === 'string' ? client.endpoint_profile : undefined,
    is_agentos: model.is_agentos === true,
    source: typeof model.source === 'string' ? model.source : undefined,
    read_only: model.read_only === true,
  };
}

export function buildModelResourcePayload(model: ModelEntry): Record<string, unknown> {
  const client: Record<string, unknown> = {
    model_name: model.model_name,
    api_base: model.api_base,
    client_provider: model.model_provider,
  };
  if (model.api_key.trim()) client.api_key = model.api_key;
  if (model.timeout !== undefined) client.timeout = model.timeout;
  if (model.vendor_key) client.vendor_key = model.vendor_key;
  if (model.plan) client.plan = model.plan;
  if (model.endpoint_profile) client.endpoint_profile = model.endpoint_profile;
  const config: Record<string, unknown> = {};
  if (model.temperature !== undefined) config.temperature = model.temperature;
  config.reasoning_level = model.reasoning_level ?? '';
  if (model.context_window_tokens !== undefined) config.context_window = model.context_window_tokens;
  return {
    ...(model.model_id ? { model_id: model.model_id } : {}),
    alias: model.alias ?? '',
    is_default: model.is_default === true,
    model_client_config: client,
    model_config_obj: config,
  };
}

export type ModelSettingsTabId = 'models' | 'groups';

type ModelsSettingsProps = {
  tabs: readonly SettingsTabDefinition<ModelSettingsTabId>[];
  defaultTab: ModelSettingsTabId;
};

function modelIdentity(model: ModelEntry, index: number): string {
  return `${model.model_id ?? model.origin_index ?? `new-${index}`}:${model.model_name}:${model.alias ?? ''}`;
}

export function parseModelsPayload(payload: unknown): {
  models: ModelEntry[];
  modelGroups: ModelGroup[];
  activeModel?: string;
} {
  if (!payload || typeof payload !== 'object' || Array.isArray(payload)) throw new Error('INVALID_MODELS_LIST');
  const models = (payload as { models?: unknown }).models;
  if (!Array.isArray(models)) throw new Error('INVALID_MODELS_LIST');
  if (
    !models.every(
      (model) =>
        model &&
        typeof model === 'object' &&
        !Array.isArray(model) &&
        typeof (model as ModelEntry).model_name === 'string' &&
        typeof (model as ModelEntry).api_base === 'string' &&
        ((model as ModelEntry).api_key === undefined || typeof (model as ModelEntry).api_key === 'string') &&
        ((model as ModelEntry).has_api_key === undefined || typeof (model as ModelEntry).has_api_key === 'boolean') &&
        typeof (model as ModelEntry).model_provider === 'string' &&
        typeof (model as ModelEntry).reasoning_level === 'string',
    )
  ) {
    throw new Error('INVALID_MODELS_LIST');
  }
  const activeModel = (payload as { active_model?: unknown }).active_model;
  if (activeModel !== undefined && typeof activeModel !== 'string') throw new Error('INVALID_MODELS_LIST');
  const modelGroups = parseModelGroups((payload as { model_groups?: unknown }).model_groups);
  const normalizedModels = (models as ModelEntry[]).map((model) => ({
    ...model,
    api_key: typeof model.api_key === 'string' ? model.api_key : '',
  }));
  return { models: normalizedModels, modelGroups, activeModel };
}

export function ModelsSettings({ tabs, defaultTab }: ModelsSettingsProps) {
  const { t } = useTranslation();
  const { isConnected, request, saveQueue } = useSettingsServices();
  const setAvailableModels = useSessionStore((state) => state.setAvailableModels);
  const setAvailableModelGroups = useSessionStore((state) => state.setAvailableModelGroups);
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [modelGroups, setModelGroups] = useState<ModelGroup[]>([]);
  const [catalog, setCatalog] = useState<VendorPresetMap>(EMPTY_VENDOR_CATALOG);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const [catalogError, setCatalogError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [dialog, setDialog] = useState<{ model?: ModelEntry } | null>(null);
  const [groupDialog, setGroupDialog] = useState<{ group?: ModelGroup } | null>(null);
  const [confirmation, setConfirmation] = useState<ModelConfirmation | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState('');
  const [validationStates, setValidationStates] = useState<Record<string, 'testing' | 'success'>>({});
  const [validationToast, setValidationToast] = useState<ValidationToast | null>(null);
  const modelsRequestId = useRef(0);
  const catalogRequestId = useRef(0);
  const validationRequestIds = useRef<Record<string, number>>({});
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const editableModels = useMemo(() => getEditableModels(models), [models]);
  const hasModelsError = Boolean(modelsError);
  const actionsDisabled = !isConnected || modelsLoading || hasModelsError || saving;

  const applyCatalog = useCallback(
    (parsed: { models: ModelEntry[]; modelGroups: ModelGroup[]; activeModel?: string }) => {
      setModels(parsed.models);
      setModelGroups(parsed.modelGroups);
      setAvailableModels(parsed.models, parsed.activeModel);
      setAvailableModelGroups(parsed.modelGroups);
    },
    [setAvailableModelGroups, setAvailableModels],
  );

  const loadModels = useCallback(async () => {
    if (!isConnected) {
      setModelsLoading(false);
      return;
    }
    const currentRequestId = ++modelsRequestId.current;
    setModelsLoading(true);
    setModelsError('');
    try {
      const payload = await request('models.list');
      if (currentRequestId !== modelsRequestId.current) return;
      applyCatalog(parseModelsPayload(payload));
    } catch (error) {
      if (currentRequestId === modelsRequestId.current) {
        setModelsError(
          error instanceof Error && error.message !== 'INVALID_MODELS_LIST'
            ? error.message
            : t('settingsPanel.models.modelsResponseInvalid'),
        );
      }
    } finally {
      if (currentRequestId === modelsRequestId.current) setModelsLoading(false);
    }
  }, [applyCatalog, isConnected, request, t]);

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
      setCatalog(withoutGenerationOnlyPresets(parseVendorCatalog(payload.vendors)));
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

  const reloadModels = useCallback(async () => {
    setSaveError('');
    await loadModels();
  }, [loadModels]);

  const closeModelDialog = useCallback(() => {
    catalogRequestId.current += 1;
    setCatalogLoading(false);
    setCatalogError('');
    setDialog(null);
  }, []);

  useEffect(() => {
    void reloadModels();
    return () => {
      modelsRequestId.current += 1;
      catalogRequestId.current += 1;
      Object.keys(validationRequestIds.current).forEach((key) => {
        validationRequestIds.current[key] += 1;
      });
      if (toastTimer.current) clearTimeout(toastTimer.current);
    };
  }, [reloadModels]);

  const showValidationToast = useCallback((toast: ValidationToast) => {
    if (toastTimer.current) clearTimeout(toastTimer.current);
    setValidationToast(toast);
    toastTimer.current = setTimeout(() => {
      setValidationToast(null);
      toastTimer.current = null;
    }, 3000);
  }, []);

  const openModelDialog = useCallback(
    async (nextDialog: { model?: ModelEntry }) => {
      void loadCatalog();
      if (!nextDialog.model?.model_id) {
        setDialog(nextDialog);
        return;
      }
      try {
        const payload = await request('models.get', { model_id: nextDialog.model.model_id });
        setDialog({ model: parseModelResource(payload) });
      } catch (error) {
        showValidationToast({
          success: false,
          message: error instanceof Error ? error.message : t('settingsPanel.feedback.loadFailed'),
        });
      }
    },
    [loadCatalog, request, showValidationToast, t],
  );

  const saveModel = async (
    model: ModelEntry,
    operation: string,
    { errorScope = 'page' }: SaveModelsOptions = {},
  ) => {
    setSaving(true);
    setSaveError('');
    try {
      const result = await saveQueue.enqueue(
        operation,
        () =>
          request<ModelResourceResult>('models.upsert', { model: buildModelResourcePayload(model) }, {
            timeoutMs: 600_000,
          }),
        { errorScope },
      );
      if (typeof result.applied_without_restart !== 'boolean') {
        throw new Error(t('settingsPanel.models.saveResponseInvalid'));
      }
      const refreshedPayload = await request('models.list');
      applyCatalog(parseModelsPayload(refreshedPayload));
      showValidationToast({
        success: true,
        message: t(
          result.applied_without_restart
            ? 'settingsPanel.models.savedApplied'
            : 'settingsPanel.models.savedRestartRequired',
        ),
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed');
      if (errorScope === 'page') setSaveError(message);
      throw error;
    } finally {
      setSaving(false);
    }
  };

  const requestDeleteModel = (model: ModelEntry) => {
    if (!model.model_id) return;
    setConfirmError('');
    setConfirmation({
      type: 'delete-model',
      model,
      message: t('settingsPanel.models.deleteConfirm', {
        alias: model.alias || t('settingsPanel.models.missingCustomName'),
        model: model.model_name,
      }),
    });
  };

  const saveModelGroup = async (group: ModelGroup) => {
    setSaving(true);
    setSaveError('');
    try {
      const result = await saveQueue.enqueue(
        group.model_group_id ? 'model_group.edit' : 'model_group.add',
        () => request<{ group?: unknown }>('model_groups.upsert', { group }, { timeoutMs: 600_000 }),
        { errorScope: 'caller' },
      );
      const savedGroup = parseModelGroup(result?.group);
      if (savedGroup) {
        setModelGroups((current) => {
          const without = current.filter((item) => item.model_group_id !== savedGroup.model_group_id);
          return [...without, savedGroup];
        });
      }
      const refreshedPayload = await request('models.list');
      applyCatalog(parseModelsPayload(refreshedPayload));
      showValidationToast({
        success: true,
        message: t('settingsPanel.models.savedApplied'),
      });
    } catch (error) {
      const message = error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed');
      throw new Error(message);
    } finally {
      setSaving(false);
    }
  };

  const requestDeleteModelGroup = (group: ModelGroup) => {
    if (!group.model_group_id) return;
    setConfirmError('');
    setConfirmation({
      type: 'delete-group',
      group,
      message: t('settingsPanel.models.deleteModelGroupConfirm', {
        name: getModelGroupDisplayName(group),
      }),
    });
  };

  const confirmModelOperation = async () => {
    if (!confirmation || confirming || saving) return;
    setConfirming(true);
    setConfirmError('');
    try {
      if (confirmation.type === 'delete-model') {
        if (!confirmation.model.model_id) throw new Error(t('settingsPanel.feedback.saveFailed'));
        setSaving(true);
        await saveQueue.enqueue(
          'model.delete',
          () => request('models.delete', { model_id: confirmation.model.model_id }, { timeoutMs: 600_000 }),
          { errorScope: 'page' },
        );
        const refreshedPayload = await request('models.list');
        applyCatalog(parseModelsPayload(refreshedPayload));
        showValidationToast({ success: true, message: t('settingsPanel.models.savedApplied') });
      } else if (confirmation.type === 'remove-group-route') {
        const nextGroup = removeGroupRoute(confirmation.group, confirmation.routeId);
        if (nextGroup.routes.length === 0) {
          throw new Error(t('settingsPanel.models.keepOneGroupRoute'));
        }
        await saveModelGroup(nextGroup);
      } else if (confirmation.group.model_group_id) {
        setSaving(true);
        await saveQueue.enqueue(
          'model_group.delete',
          () =>
            request(
              'model_groups.delete',
              { model_group_id: confirmation.group.model_group_id },
              {
                timeoutMs: 600_000,
              },
            ),
          { errorScope: 'page' },
        );
        const refreshedPayload = await request('models.list');
        applyCatalog(parseModelsPayload(refreshedPayload));
        showValidationToast({ success: true, message: t('settingsPanel.models.savedApplied') });
      }
      setConfirmation(null);
    } catch (error) {
      setConfirmError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setConfirming(false);
      setSaving(false);
    }
  };

  const setGroupRouteDefault = async (group: ModelGroup, routeId: string) => {
    if (getGroupDefaultRouteId(group) === routeId) return;
    await saveModelGroup(promoteGroupRouteDefault(group, routeId));
  };

  const testSavedModel = async (model: ModelEntry, index: number) => {
    const key = modelIdentity(model, index);
    const requestId = (validationRequestIds.current[key] ?? 0) + 1;
    validationRequestIds.current[key] = requestId;
    setValidationStates((current) => ({ ...current, [key]: 'testing' }));
    try {
      await request('config.validate_model', buildModelValidationPayload(model), { timeoutMs: 60_000 });
      if (validationRequestIds.current[key] !== requestId) return;
      setValidationStates((current) => ({ ...current, [key]: 'success' }));
      showValidationToast({ success: true, message: t('settingsPanel.models.validationOk') });
    } catch (error) {
      if (validationRequestIds.current[key] !== requestId) return;
      setValidationStates((current) => {
        const next = { ...current };
        delete next[key];
        return next;
      });
      const detail = error instanceof Error ? error.message.trim() : '';
      showValidationToast({
        success: false,
        message: detail
          ? t('settingsPanel.models.validationFailedWithDetail', { detail })
          : t('settingsPanel.models.validationFailed'),
      });
    }
  };

  const getCardPresentation = (model: ModelEntry) => {
    const customName = model.alias?.trim() || model.model_name;
    const protocol = displayModelProtocol(model);
    const protocolLabel = t(`settingsPanel.models.protocols.${protocol}`);
    if (model.model_provider === 'OpenAIAccount') {
      return {
        customName,
        metadata: [t('settingsPanel.models.openaiAccount'), protocolLabel, model.model_name].join(' · '),
        logo: getModelLogoUrl(model),
      };
    }
    const vendorKey = model.vendor_key?.trim();
    const providerLabel = vendorKey ? getVendorLabel(vendorKey, t) : t('settingsPanel.models.customVendor');
    return {
      customName,
      metadata: [providerLabel, protocolLabel, model.model_name].join(' · '),
      logo: getModelLogoUrl(model),
    };
  };

  const renderModelCard = (model: ModelEntry, index: number) => {
    if (isRuntimeGrantedModel(model)) return null;
    const key = modelIdentity(model, index);
    const validationState = validationStates[key];
    const presentation = getCardPresentation(model);
    const readOnly = model.is_agentos === true;
    return (
      <article
        className={`settings-model-card${presentation.logo ? '' : ' settings-model-card--no-logo'}`}
        key={key}
        data-testid="settings-models-card"
        data-variant={model.model_id ?? model.origin_index ?? 'new'}
      >
        {presentation.logo ? (
          <img className="settings-model-card__logo" src={presentation.logo} alt="" aria-hidden />
        ) : null}
        <div className="settings-model-card__copy">
          <div className="settings-model-card__title-row">
            <h3
              title={presentation.customName}
              data-testid="settings-models-card-title"
              data-variant={model.model_id ?? model.origin_index ?? 'new'}
            >
              {presentation.customName}
            </h3>
            {readOnly ? (
              <Tag
                variant="neutral"
                data-testid="settings-models-card-readonly-tag"
                data-variant={model.model_id ?? model.origin_index ?? 'new'}
              >
                {t('settingsPanel.models.agentOsReadonly')}
              </Tag>
            ) : null}
          </div>
          <p
            title={presentation.metadata}
            data-testid="settings-models-card-metadata"
            data-variant={model.model_id ?? model.origin_index ?? 'new'}
          >
            {presentation.metadata}
          </p>
        </div>
        <div className="settings-model-card__actions">
          <Button
            icon={<settingsActionIcons.test aria-hidden />}
            aria-label={t('settingsPanel.models.testConnection')}
            title={t('settingsPanel.models.testConnection')}
            loading={validationState === 'testing'}
            disabled={actionsDisabled}
            onClick={() => void testSavedModel(model, index)}
            data-testid="settings-models-card-test-btn"
            data-variant={model.model_id ?? model.origin_index ?? 'new'}
          />
          {!readOnly ? (
            <Button
              icon={<settingsActionIcons.edit aria-hidden />}
              aria-label={t('common.modify')}
              title={t('common.modify')}
              disabled={actionsDisabled}
              onClick={() => void openModelDialog({ model })}
              data-testid="settings-models-card-edit-btn"
              data-variant={model.model_id ?? model.origin_index ?? 'new'}
            />
          ) : null}
          {!readOnly ? (
            <Button
              variant="quiet"
              icon={<settingsActionIcons.delete aria-hidden />}
              aria-label={t('common.delete')}
              title={editableModels.length <= 1 ? t('settingsPanel.models.keepOneModel') : t('common.delete')}
              disabled={actionsDisabled || editableModels.length <= 1}
              onClick={() => void requestDeleteModel(model)}
              data-testid="settings-models-card-delete-btn"
              data-variant={model.model_id ?? model.origin_index ?? 'new'}
            />
          ) : null}
        </div>
      </article>
    );
  };

  return (
    <>
      <SettingsTabLayout
        tabs={tabs}
        defaultTab={defaultTab}
        ariaLabelKey="settingsPanel.models.tabsLabel"
        className="settings-models__tabs"
        testIdPrefix="settings-models"
        renderPanel={(activeTab) => activeTab === 'models' ? (
        <div className={'settings-models__models-pane'}>
          <FreeModelsSettings />
          <SettingsSection
            title={t('settingsPanel.models.customModels')}
            separatedRows
            action={
              <Button
                variant="primary"
                disabled={actionsDisabled}
                onClick={() => void openModelDialog({})}
                data-testid="settings-models-add-btn"
              >
                {t('settingsPanel.models.addModel')}
              </Button>
            }
          >
            {modelsError ? (
              <div className="settings-page__error" role="alert" data-testid="settings-models-error">
                {modelsError}
              </div>
            ) : null}
            {saveError ? (
              <div
                className="settings-page__error settings-models__save-error"
                role="alert"
                data-testid="settings-models-save-error"
              >
                <span>{saveError}</span>
                <Button
                  size="sm"
                  disabled={!isConnected || modelsLoading || saving}
                  onClick={() => void reloadModels()}
                  data-testid="settings-models-reload-btn"
                >
                  {t('settingsPanel.models.reloadAfterFailure')}
                </Button>
              </div>
            ) : null}
            {!hasModelsError && !modelsLoading && editableModels.length === 0 ? (
              <div className="settings-models__empty" data-testid="settings-models-empty">
                <img src={settingsEmptyBoxIllustration} alt="" aria-hidden />
                <strong>{t('settingsPanel.models.empty')}</strong>
                <p>{t('settingsPanel.models.emptyDescription')}</p>
                <Button
                  variant="primary"
                  disabled={!isConnected || saving}
                  onClick={() => void openModelDialog({})}
                  data-testid="settings-models-empty-add-btn"
                >
                  {t('settingsPanel.models.addModel')}
                </Button>
              </div>
            ) : null}
            <div className="settings-models__list" aria-busy={saving || undefined} data-testid="settings-models-list">
              {!hasModelsError && !modelsLoading ? models.map((model, index) => renderModelCard(model, index)) : null}
            </div>
          </SettingsSection>
        </div>
        ) : (
        <ModelGroupsSettings
          models={models}
          modelGroups={modelGroups}
          loading={modelsLoading}
          hasError={hasModelsError}
          saving={saving}
          actionsDisabled={actionsDisabled}
          getModelPresentation={getCardPresentation}
          getValidationState={(model, index) => validationStates[modelIdentity(model, index)]}
          onAdd={() => setGroupDialog({})}
          onEdit={(group) => setGroupDialog({ group })}
          onDelete={(group) => void requestDeleteModelGroup(group)}
          onSetDefault={setGroupRouteDefault}
          onTestModel={(model, index) => void testSavedModel(model, index)}
          onRemoveRoute={(group, routeId, modelName) => {
            setConfirmError('');
            setConfirmation({
              type: 'remove-group-route',
              group,
              routeId,
              message: t('settingsPanel.models.removeModelFromGroupConfirm', { name: modelName }),
            });
          }}
        />
        )}
      />
      {groupDialog ? (
        <ModelGroupDialog
          models={models}
          group={groupDialog.group}
          saving={saving}
          onClose={() => setGroupDialog(null)}
          onSave={saveModelGroup}
        />
      ) : null}
      {dialog ? (
        <ModelDialog
          model={dialog.model}
          models={models}
          catalog={catalog}
          catalogLoading={catalogLoading}
          catalogError={catalogError}
          saving={saving}
          onRetryCatalog={() => void loadCatalog()}
          onClose={closeModelDialog}
          onSave={async (next) => {
            await saveModel(next, dialog.model ? 'model.edit' : 'model.add', { errorScope: 'caller' });
          }}
        />
      ) : null}
      <SettingsConfirmDialog
        open={confirmation !== null}
        title={t(
          confirmation?.type === 'delete-group'
            ? 'settingsPanel.models.deleteModelGroupConfirmTitle'
            : confirmation?.type === 'remove-group-route'
              ? 'settingsPanel.models.removeModelFromGroupConfirmTitle'
              : 'settingsPanel.models.deleteConfirmTitle',
        )}
        message={confirmation?.message ?? ''}
        confirming={confirming}
        error={confirmError}
        onCancel={() => {
          if (!confirming) setConfirmation(null);
        }}
        onConfirm={() => void confirmModelOperation()}
      />
      {validationToast ? (
        <div
          className={`settings-models__toast settings-models__toast--${validationToast.success ? 'success' : 'error'}`}
          role={validationToast.success ? 'status' : 'alert'}
          aria-live="polite"
          data-testid="settings-models-toast"
          data-variant={validationToast.success ? 'success' : 'error'}
        >
          {validationToast.success ? <Check aria-hidden /> : null}
          <span>{validationToast.message}</span>
        </div>
      ) : null}
    </>
  );
}
