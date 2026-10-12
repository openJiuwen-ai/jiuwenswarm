import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, ModelGroup, VendorPresetMap } from '../../../../types';
import { Button, Tabs, Tag } from '../../../../components/ui';
import { settingsActionIcons, settingsEmptyBoxIllustration } from '../../../../assets/settings';
import { getModelLogoUrl } from '../../../../assets/providers';
import { buildModelValidationPayload, buildModelsSavePayload } from '../../services/settingsContract';
import type { SettingsSaveErrorScope } from '../../services/SettingsSaveQueue';
import { SettingsConfirmDialog, SettingsSection } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { FreeModelsSettings } from './FreeModelsSettings';
import { ModelDialog } from './ModelDialog';
import { ModelGroupDialog } from './ModelGroupDialog';
import { ModelGroupsSettings } from './ModelGroupsSettings';
import { getVendorLabel } from './ModelProviderSelect';
import { displayModelProtocol, parseVendorCatalog } from './modelAdapters';
import {
  extractModelSelectionReferences,
  getGroupDefaultModelId,
  getModelGroupDisplayName,
  parseModelGroup,
  parseModelGroups,
  parseModelSelectionReferences,
  promoteGroupRouteDefault,
  removeGroupRoute,
} from './modelGroupAdapters';
import { useSessionStore } from '../../../../stores/sessionStore';
import { addEditableModel, getEditableModels, promotePrimaryModel, removeEditableModel } from './modelListOperations';

const EMPTY_VENDOR_CATALOG: VendorPresetMap = {
  reasoning: null,
  token_plan: [],
  coding_plan: [],
  custom_api: [],
};

type ModelConfirmation =
  | { type: 'delete-model'; model: ModelEntry; message: string }
  | { type: 'delete-group'; group: ModelGroup; message: string }
  | { type: 'remove-group-route'; group: ModelGroup; modelId: string; message: string };

type ValidationToast = { success: boolean; message: string };
type ReplaceModelsResult = { count: number; applied_without_restart: boolean };
type SaveModelsOptions = { errorScope?: SettingsSaveErrorScope };

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

export function ModelsSettings() {
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
  const [activeTab, setActiveTab] = useState<'models' | 'groups'>('models');
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

  const reloadModels = useCallback(async () => {
    setSaveError('');
    await loadModels();
  }, [loadModels]);

  const openModelDialog = useCallback(
    (nextDialog: { model?: ModelEntry }) => {
      setDialog(nextDialog);
      void loadCatalog();
    },
    [loadCatalog],
  );

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

  const saveModels = async (
    nextModels: ModelEntry[],
    operation: string,
    { errorScope = 'page' }: SaveModelsOptions = {},
  ) => {
    const submittedModels = getEditableModels(nextModels);
    setSaving(true);
    setSaveError('');
    try {
      const result = await saveQueue.enqueue(
        operation,
        () =>
          request<ReplaceModelsResult>('models.replace_all', buildModelsSavePayload(submittedModels), {
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

  const requestDeleteModelGroup = async (group: ModelGroup) => {
    if (!group.model_group_id) return;
    setConfirmError('');
    try {
      const referencesPayload = await request('models.references', {
        type: 'model_group',
        id: group.model_group_id,
      });
      const references = parseModelSelectionReferences(referencesPayload);
      if (references.length > 0) {
        showValidationToast({
          success: false,
          message: t('settingsPanel.models.modelGroupReferenced', {
            count: references.length,
            scopes: references.map((item) => item.scope).join(', '),
          }),
        });
        return;
      }
      setConfirmation({
        type: 'delete-group',
        group,
        message: t('settingsPanel.models.deleteModelGroupConfirm', {
          name: getModelGroupDisplayName(group),
        }),
      });
    } catch (error) {
      const referenced = extractModelSelectionReferences(error);
      if (referenced.length > 0 || (error as { code?: string })?.code === 'MODEL_SELECTION_REFERENCED') {
        showValidationToast({
          success: false,
          message: t('settingsPanel.models.modelGroupReferenced', {
            count: referenced.length || 1,
            scopes: referenced.map((item) => item.scope).join(', ') || 'unknown',
          }),
        });
        return;
      }
      showValidationToast({
        success: false,
        message: error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'),
      });
    }
  };

  const confirmModelOperation = async () => {
    if (!confirmation || confirming || saving) return;
    setConfirming(true);
    setConfirmError('');
    try {
      if (confirmation.type === 'delete-model') {
        await saveModels(removeEditableModel(models, confirmation.model), 'model.delete');
      } else if (confirmation.type === 'remove-group-route') {
        const nextGroup = removeGroupRoute(confirmation.group, confirmation.modelId);
        if (nextGroup.routes.length === 0) {
          throw new Error(t('settingsPanel.models.keepOneGroupRoute'));
        }
        await saveModelGroup(nextGroup);
      } else if (confirmation.group.model_group_id) {
        setSaving(true);
        try {
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
        } catch (error) {
          const referenced = extractModelSelectionReferences(error);
          if (referenced.length > 0 || (error as { code?: string })?.code === 'MODEL_SELECTION_REFERENCED') {
            throw new Error(
              t('settingsPanel.models.modelGroupReferenced', {
                count: referenced.length || 1,
                scopes: referenced.map((item) => item.scope).join(', ') || 'unknown',
              }),
            );
          }
          throw error;
        }
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

  const setGroupRouteDefault = async (group: ModelGroup, modelId: string) => {
    if (getGroupDefaultModelId(group) === modelId) return;
    await saveModelGroup(promoteGroupRouteDefault(group, modelId));
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

  const setPrimary = async (model: ModelEntry) => {
    await saveModels(promotePrimaryModel(models, model), 'model.primary');
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
    const key = modelIdentity(model, index);
    const validationState = validationStates[key];
    const presentation = getCardPresentation(model);
    const isPrimary = model === editableModels[0];
    const readOnly = model.is_agentos === true;
    const canSetPrimary = !readOnly && !isPrimary;
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
            {isPrimary ? (
              <Tag
                variant="info"
                data-testid="settings-models-card-primary-tag"
                data-variant={model.model_id ?? model.origin_index ?? 'new'}
              >
                {t('settingsPanel.models.primary')}
              </Tag>
            ) : null}
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
          {canSetPrimary ? (
            <Button
              className="settings-model-card__text-action"
              variant="quiet"
              size="sm"
              disabled={actionsDisabled}
              onClick={() => void setPrimary(model).catch(() => undefined)}
              data-testid="settings-models-card-set-primary-btn"
              data-variant={model.model_id ?? model.origin_index ?? 'new'}
            >
              {t('settingsPanel.models.setPrimary')}
            </Button>
          ) : null}
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
              onClick={() => openModelDialog({ model })}
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
              onClick={() => {
                setConfirmError('');
                setConfirmation({
                  type: 'delete-model',
                  model,
                  message: t('settingsPanel.models.deleteConfirm', {
                    alias: model.alias || t('settingsPanel.models.missingCustomName'),
                    model: model.model_name,
                  }),
                });
              }}
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
      <Tabs
        role="tablist"
        ariaLabel={t('settingsPanel.models.tabsLabel')}
        bordered
        className="settings-models__tabs"
        wrapperTestId="settings-models-tabs"
        itemTestId="settings-models-tab"
        value={activeTab}
        onChange={setActiveTab}
        items={[
          { value: 'models', label: t('settingsPanel.models.modelsTab') },
          { value: 'groups', label: t('settingsPanel.models.modelGroups') },
        ]}
      />
      {activeTab === 'models' ? (
        <div className={'settings-models__models-pane'}>
          <FreeModelsSettings />
          <SettingsSection
            title={t('settingsPanel.models.customModels')}
            separatedRows
            action={
              <Button
                variant="primary"
                disabled={actionsDisabled}
                onClick={() => openModelDialog({})}
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
                  onClick={() => openModelDialog({})}
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
          onRemoveRoute={(group, modelId, modelName) => {
            setConfirmError('');
            setConfirmation({
              type: 'remove-group-route',
              group,
              modelId,
              message: t('settingsPanel.models.removeModelFromGroupConfirm', { name: modelName }),
            });
          }}
        />
      )}
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
            const nextModels = dialog.model
              ? models.map((current) => (current === dialog.model ? next : current))
              : addEditableModel(models, next);
            await saveModels(nextModels, dialog.model ? 'model.edit' : 'model.add', { errorScope: 'caller' });
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
