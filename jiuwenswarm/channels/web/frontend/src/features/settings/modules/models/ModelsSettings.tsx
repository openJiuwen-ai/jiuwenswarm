import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { Check, ChevronRight } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, VendorPresetMap } from '../../../../types';
import { Button, Tag } from '../../../../components/ui';
import {
  settingsActionIcons,
  settingsEmptyBoxIllustration,
} from '../../../../assets/settings';
import { getModelLogoUrl } from '../../../../assets/providers';
import { buildModelValidationPayload, buildModelsSavePayload } from '../../services/settingsContract';
import type { SettingsSaveErrorScope } from '../../services/SettingsSaveQueue';
import { SettingsConfirmDialog, SettingsSection } from '../../components';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { ModelDialog } from './ModelDialog';
import { getVendorLabel } from './ModelProviderSelect';
import { displayModelProtocol, parseVendorCatalog } from './modelAdapters';
import { useSessionStore } from '../../../../stores/sessionStore';
import {
  getEditableModels,
  getModelDisplayGroups,
  promotePrimaryModel,
  removeEditableModel,
  setGroupDefaultModel,
} from './modelListOperations';

const EMPTY_VENDOR_CATALOG: VendorPresetMap = {
  token_plan: [],
  coding_plan: [],
  custom_api: [],
};

type ModelConfirmation =
  | { type: 'group-default'; model: ModelEntry; message: string }
  | { type: 'delete'; model: ModelEntry; message: string };

type ValidationToast = { success: boolean; message: string };
type ReplaceModelsResult = { count: number; applied_without_restart: boolean };
type SaveModelsOptions = { errorScope?: SettingsSaveErrorScope };

function modelIdentity(model: ModelEntry, index: number): string {
  return `${model.origin_index ?? `new-${index}`}:${model.model_name}:${model.alias ?? ''}`;
}

function parseModelsPayload(payload: unknown): { models: ModelEntry[]; activeModel?: string } {
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
        typeof (model as ModelEntry).api_key === 'string' &&
        typeof (model as ModelEntry).model_provider === 'string',
    )
  ) {
    throw new Error('INVALID_MODELS_LIST');
  }
  const activeModel = (payload as { active_model?: unknown }).active_model;
  if (activeModel !== undefined && typeof activeModel !== 'string') throw new Error('INVALID_MODELS_LIST');
  return { models: models as ModelEntry[], activeModel };
}

export function ModelsSettings() {
  const { t } = useTranslation();
  const { isConnected, request, saveQueue } = useSettingsServices();
  const setAvailableModels = useSessionStore((state) => state.setAvailableModels);
  const [models, setModels] = useState<ModelEntry[]>([]);
  const [catalog, setCatalog] = useState<VendorPresetMap>(EMPTY_VENDOR_CATALOG);
  const [modelsLoading, setModelsLoading] = useState(true);
  const [catalogLoading, setCatalogLoading] = useState(false);
  const [modelsError, setModelsError] = useState('');
  const [catalogError, setCatalogError] = useState('');
  const [saveError, setSaveError] = useState('');
  const [saving, setSaving] = useState(false);
  const [dialog, setDialog] = useState<{ model?: ModelEntry } | null>(null);
  const [confirmation, setConfirmation] = useState<ModelConfirmation | null>(null);
  const [confirming, setConfirming] = useState(false);
  const [confirmError, setConfirmError] = useState('');
  const [validationStates, setValidationStates] = useState<Record<string, 'testing' | 'success'>>({});
  const [validationToast, setValidationToast] = useState<ValidationToast | null>(null);
  const [expandedModelGroups, setExpandedModelGroups] = useState<Record<string, boolean>>({});
  const modelsRequestId = useRef(0);
  const catalogRequestId = useRef(0);
  const validationRequestIds = useRef<Record<string, number>>({});
  const toastTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  const editableModels = useMemo(() => getEditableModels(models), [models]);
  const modelDisplayGroups = useMemo(() => getModelDisplayGroups(models), [models]);
  const hasModelsError = Boolean(modelsError);
  const actionsDisabled = !isConnected || modelsLoading || hasModelsError || saving;

  const toggleModelGroup = useCallback((modelName: string) => {
    setExpandedModelGroups((current) => ({ ...current, [modelName]: !current[modelName] }));
  }, []);

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
      const parsed = parseModelsPayload(payload);
      setModels(parsed.models.filter((model) => model.is_free !== true));
      setAvailableModels(parsed.models, parsed.activeModel);
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
  }, [isConnected, request, setAvailableModels, t]);

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
      const parsed = parseModelsPayload(refreshedPayload);
      setModels(parsed.models.filter((model) => model.is_free !== true));
      setAvailableModels(parsed.models, parsed.activeModel);
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

  const confirmModelOperation = async () => {
    if (!confirmation || confirming || saving) return;
    setConfirming(true);
    setConfirmError('');
    try {
      if (confirmation.type === 'delete') {
        await saveModels(removeEditableModel(models, confirmation.model), 'model.delete');
      } else {
        await saveModels(setGroupDefaultModel(models, confirmation.model), 'model.group_default');
      }
      setConfirmation(null);
    } catch (error) {
      setConfirmError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setConfirming(false);
    }
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
      showValidationToast({
        success: false,
        message: error instanceof Error ? error.message : t('settingsPanel.models.validationFailed'),
      });
    }
  };

  const setPrimary = async (model: ModelEntry) => {
    await saveModels(promotePrimaryModel(models, model), 'model.primary');
  };

  const getCardPresentation = (model: ModelEntry, groupOrdinal?: number) => {
    const baseName = model.alias?.trim() || model.model_name;
    const customName = groupOrdinal === undefined ? baseName : `${baseName} #${groupOrdinal}`;
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

  const renderModelCard = (model: ModelEntry, index: number, groupOrdinal?: number) => {
    const key = modelIdentity(model, index);
    const validationState = validationStates[key];
    const isDuplicate = groupOrdinal !== undefined;
    const presentation = getCardPresentation(model, groupOrdinal);
    const isPrimary = model === editableModels[0];
    const readOnly = model.is_agentos === true;
    const canSetPrimary = !readOnly && !isPrimary && (!isDuplicate || model.is_default === true);
    return (
      <article
        className={`settings-model-card${isDuplicate ? ' settings-model-card--grouped' : ''}${presentation.logo ? '' : ' settings-model-card--no-logo'}`}
        key={key}
      >
        {presentation.logo ? (
          <img className="settings-model-card__logo" src={presentation.logo} alt="" aria-hidden />
        ) : null}
        <div className="settings-model-card__copy">
          <div className="settings-model-card__title-row">
            <h3 title={presentation.customName}>{presentation.customName}</h3>
            {isPrimary ? <Tag variant="info">{t('settingsPanel.models.primary')}</Tag> : null}
            {isDuplicate && model.is_default ? (
              <Tag variant="neutral">{t('settingsPanel.models.groupDefault')}</Tag>
            ) : null}
            {readOnly ? <Tag variant="neutral">{t('settingsPanel.models.agentOsReadonly')}</Tag> : null}
          </div>
          <p title={presentation.metadata}>{presentation.metadata}</p>
        </div>
        <div className="settings-model-card__actions">
          {canSetPrimary ? (
            <Button
              className="settings-model-card__text-action"
              variant="quiet"
              size="sm"
              disabled={actionsDisabled}
              onClick={() => void setPrimary(model).catch(() => undefined)}
            >
              {t('settingsPanel.models.setPrimary')}
            </Button>
          ) : null}
          {!readOnly && isDuplicate && !model.is_default ? (
            <Button
              className="settings-model-card__text-action"
              variant="quiet"
              size="sm"
              disabled={actionsDisabled}
              onClick={() => {
                const isPrimaryGroup = model.model_name === editableModels[0]?.model_name;
                setConfirmError('');
                setConfirmation({
                  type: 'group-default',
                  model,
                  message: t(
                    isPrimaryGroup
                      ? 'settingsPanel.models.setPrimaryGroupDefaultConfirm'
                      : 'settingsPanel.models.setGroupDefaultConfirm',
                    { model: model.model_name },
                  ),
                });
              }}
            >
              {t('settingsPanel.models.setGroupDefault')}
            </Button>
          ) : null}
          <Button
            icon={<settingsActionIcons.refresh aria-hidden />}
            aria-label={t('settingsPanel.models.testConnection')}
            title={t('settingsPanel.models.testConnection')}
            loading={validationState === 'testing'}
            disabled={actionsDisabled}
            onClick={() => void testSavedModel(model, index)}
          />
          {!readOnly ? (
            <Button
              icon={<settingsActionIcons.edit aria-hidden />}
              aria-label={t('common.modify')}
              title={t('common.modify')}
              disabled={actionsDisabled}
              onClick={() => openModelDialog({ model })}
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
                  type: 'delete',
                  model,
                  message: t('settingsPanel.models.deleteConfirm', {
                    alias: model.alias || t('settingsPanel.models.missingCustomName'),
                    model: model.model_name,
                  }),
                });
              }}
            />
          ) : null}
        </div>
      </article>
    );
  };

  return (
    <>
      <SettingsSection
        title={t('settingsPanel.models.primaryModels')}
        separatedRows
        action={
          <Button variant="primary" disabled={actionsDisabled} onClick={() => openModelDialog({})}>
            {t('settingsPanel.models.addModel')}
          </Button>
        }
      >
        {modelsError ? (
          <div className="settings-page__error" role="alert">
            {modelsError}
          </div>
        ) : null}
        {saveError ? (
          <div className="settings-page__error settings-models__save-error" role="alert">
            <span>{saveError}</span>
            <Button size="sm" disabled={!isConnected || modelsLoading || saving} onClick={() => void reloadModels()}>
              {t('settingsPanel.models.reloadAfterFailure')}
            </Button>
          </div>
        ) : null}
        {!hasModelsError && !modelsLoading && editableModels.length === 0 ? (
          <div className="settings-models__empty">
            <img src={settingsEmptyBoxIllustration} alt="" aria-hidden />
            <strong>{t('settingsPanel.models.empty')}</strong>
            <p>{t('settingsPanel.models.emptyDescription')}</p>
            <Button variant="primary" disabled={!isConnected || saving} onClick={() => openModelDialog({})}>
              {t('settingsPanel.models.addModel')}
            </Button>
          </div>
        ) : null}
        <div className="settings-models__list" aria-busy={saving || undefined}>
          {!hasModelsError && !modelsLoading
            ? modelDisplayGroups.map((group, displayGroupIndex) => {
                if (group.items.length === 1) {
                  const [{ model, index }] = group.items;
                  return renderModelCard(model, index);
                }
                const defaultItem = group.items.find(({ model }) => model.is_default === true)!;
                const defaultOrdinal = group.items.indexOf(defaultItem) + 1;
                const alternativeItems = group.items.filter((item) => item !== defaultItem);
                const isExpanded = expandedModelGroups[group.modelName] === true;
                const groupContentId = `settings-model-group-${displayGroupIndex}-alternatives`;
                return (
                  <section
                    className="settings-model-group"
                    aria-label={t('settingsPanel.models.groupAriaLabel', {
                      model: group.modelName,
                      count: group.items.length,
                    })}
                    key={`group:${group.modelName}`}
                  >
                    <button
                      type="button"
                      className="settings-model-group__header"
                      aria-expanded={isExpanded}
                      aria-controls={groupContentId}
                      aria-label={t(
                        isExpanded ? 'settingsPanel.models.collapseGroup' : 'settingsPanel.models.expandGroup',
                        { model: group.modelName },
                      )}
                      onClick={() => toggleModelGroup(group.modelName)}
                    >
                      <div className="settings-model-group__title">
                        <ChevronRight
                          className={`settings-model-group__toggle-icon${isExpanded ? ' settings-model-group__toggle-icon--expanded' : ''}`}
                          aria-hidden
                        />
                        <strong title={group.modelName}>{group.modelName}</strong>
                      </div>
                      <span className="settings-model-group__meta">
                        {t('settingsPanel.models.groupMeta', { count: group.items.length })}
                      </span>
                    </button>
                    {renderModelCard(defaultItem.model, defaultItem.index, defaultOrdinal)}
                    <div id={groupContentId} className="settings-model-group__alternatives" hidden={!isExpanded}>
                      <div className="settings-model-group__items">
                        {alternativeItems.map((item) =>
                          renderModelCard(item.model, item.index, group.items.indexOf(item) + 1),
                        )}
                      </div>
                    </div>
                  </section>
                );
              })
            : null}
        </div>
      </SettingsSection>
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
              : [...models, next];
            await saveModels(nextModels, dialog.model ? 'model.edit' : 'model.add', { errorScope: 'caller' });
          }}
        />
      ) : null}
      <SettingsConfirmDialog
        open={confirmation !== null}
        title={t(
          confirmation?.type === 'delete'
            ? 'settingsPanel.models.deleteConfirmTitle'
            : 'settingsPanel.models.groupDefaultConfirmTitle',
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
        >
          {validationToast.success ? <Check aria-hidden /> : null}
          <span>{validationToast.message}</span>
        </div>
      ) : null}
    </>
  );
}
