import { useMemo, useState, type CSSProperties } from 'react';
import { createPortal } from 'react-dom';
import { GripVertical } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, ModelGroup, ModelPlan, ModelRoute } from '../../../../types';
import { Button } from '../../../../components/ui';
import { ModelProviderIcon } from '../../../../components/ModelProviderIcon';
import { Form, FormDialog, useForm, type FormItem } from '../../../../components/form';
import { SettingsConfirmDialog } from '../../components';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { buildModelGroupUpsertPayload, getGroupCandidateModels } from './modelGroupAdapters';
import { getVendorLabel } from './ModelProviderSelect';

type ModelGroupDraft = {
  name: string;
  routes: RouteDraft[];
};

type RouteDraft = ModelRoute & { _key: string };
type ModelRouteOption = {
  key: string;
  label: string;
  modelName: string;
  vendorKey?: string;
  plan?: ModelPlan;
  model: ModelEntry;
};

function isPlanOption(option: ModelRouteOption): boolean {
  return Boolean(option.vendorKey && (option.plan === 'coding_plan' || option.plan === 'token_plan'));
}

let nextTemporaryRouteKey = 0;
function temporaryRouteKey(): string {
  nextTemporaryRouteKey += 1;
  return `new-route-${nextTemporaryRouteKey}`;
}

function moveRoute(routes: RouteDraft[], routeKey: string, nextIndex: number): RouteDraft[] {
  const index = routes.findIndex((route) => route._key === routeKey);
  if (index < 0 || nextIndex < 0 || nextIndex >= routes.length || index === nextIndex) return routes;
  const next = [...routes];
  const [item] = next.splice(index, 1);
  next.splice(nextIndex, 0, item);
  return next;
}

function ModelRouteMultiSelect({
  id,
  value,
  disabled,
  invalid,
  options,
  emptyText,
  onChange,
  onBlur,
}: {
  id: string;
  value: RouteDraft[];
  disabled: boolean;
  invalid: boolean;
  options: readonly ModelRouteOption[];
  emptyText: string;
  onChange: (next: RouteDraft[]) => void;
  onBlur: () => void;
}): JSX.Element {
  const { t } = useTranslation();
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const [openRouteKey, setOpenRouteKey] = useState<string | null>(null);
  const [activePlanKey, setActivePlanKey] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<CSSProperties | null>(null);
  const [submenuPosition, setSubmenuPosition] = useState<{ top: number; left: number } | null>(null);
  const optionByKey = new Map(options.map((option) => [option.key, option]));
  const usedModelIds = new Set(value.map((route) => route.model_id));
  const addableOption = options.find((option) => !usedModelIds.has(option.key));
  const isUnavailableModel = (modelId: string, currentId: string) => (
    modelId !== currentId && usedModelIds.has(modelId)
  );
  const planGroups = useMemo(() => {
    const groups = new Map<string, { key: string; label: string; options: ModelRouteOption[] }>();
    options.filter(isPlanOption).forEach((option) => {
      const key = `${option.plan}:${option.vendorKey}`;
      const vendorLabel = getVendorLabel(option.vendorKey!, t)
        .replace(/[（(][^）)]*[）)]/g, '')
        .replace(/\s*GLM$/i, '')
        .trim();
      const entry = groups.get(key) ?? {
        key,
        label: `${vendorLabel}–${t(`settingsPanel.models.vendorGroups.${option.plan}`)}`,
        options: [],
      };
      entry.options.push(option);
      groups.set(key, entry);
    });
    return Array.from(groups.values());
  }, [options, t]);
  const flatOptions = options.filter((option) => !isPlanOption(option));

  const replaceModel = (routeKey: string, nextId: string) => {
    const currentRoute = value.find((route) => route._key === routeKey);
    if (disabled || !nextId || !currentRoute) return;
    if (isUnavailableModel(nextId, currentRoute.model_id)) return;
    onChange(value.map((route) => route._key === routeKey ? { ...route, model_id: nextId } : route));
    setOpenRouteKey(null);
    setActivePlanKey(null);
  };

  const openPlanMenu = (planKey: string, target: HTMLElement) => {
    const anchor = target.getBoundingClientRect();
    const submenuWidth = 220;
    const left = anchor.right + 8 + submenuWidth <= window.innerWidth
      ? anchor.right + 8
      : Math.max(8, anchor.left - submenuWidth - 8);
    setSubmenuPosition({ top: Math.max(8, anchor.top), left });
    setActivePlanKey(planKey);
  };

  return (
    <div
      id={id}
      className={`settings-model-group-multiselect${invalid ? ' settings-model-group-multiselect--invalid' : ''}`}
      role="group"
      aria-disabled={disabled || undefined}
      data-testid="settings-model-group-dialog-model-multiselect"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) {
          setOpenRouteKey(null);
          setActivePlanKey(null);
          onBlur();
        }
      }}
    >
      {options.length === 0 ? (
        <p className="settings-model-group-multiselect__empty" data-testid="settings-model-group-dialog-model-multiselect-empty">
          {emptyText}
        </p>
      ) : (
        <>
          <p className="settings-model-group-multiselect__hint">
            {t('settingsPanel.models.modelGroupRouteOrderHint')}
          </p>
          <div className="settings-model-group-multiselect__table" data-testid="settings-model-group-dialog-route-order">
            <div className="settings-model-group-multiselect__table-head" aria-hidden>
              <span>{t('settingsPanel.models.modelGroupTableModel')}</span>
              <span>{t('settingsPanel.models.modelGroupTableActions')}</span>
            </div>
            <div className="settings-model-group-multiselect__selected">
              {value.map((route, index) => {
                const option = optionByKey.get(route.model_id);
                const label = option?.label || route.model_id;
                return (
                  <div
                    key={route._key}
                    className="settings-model-group-multiselect__route"
                    data-dragging={draggingId === route._key || undefined}
                    data-testid="settings-model-group-dialog-route-row"
                    data-variant={route.route_id || route._key}
                    onDragOver={(event) => {
                      if (!disabled) event.preventDefault();
                    }}
                    onDrop={(event) => {
                      event.preventDefault();
                      const sourceId = draggingId || event.dataTransfer.getData('text/plain');
                      if (sourceId) onChange(moveRoute(value, sourceId, index));
                      setDraggingId(null);
                    }}
                  >
                    <span
                      className="settings-model-group-multiselect__drag-handle"
                      aria-hidden
                      draggable={!disabled}
                      onDragStart={(event) => {
                        setDraggingId(route._key);
                        event.dataTransfer.effectAllowed = 'move';
                        event.dataTransfer.setData('text/plain', route._key);
                      }}
                      onDragEnd={() => setDraggingId(null)}
                    >
                      <GripVertical size={16} />
                    </span>
                    <div className="settings-model-group-route-picker">
                      <button
                        type="button"
                        className="settings-model-group-multiselect__route-select"
                        aria-label={t('settingsPanel.models.modelGroupTableModel')}
                        aria-haspopup="menu"
                        aria-expanded={openRouteKey === route._key}
                        disabled={disabled}
                        title={label}
                        onClick={(event) => {
                          setActivePlanKey(null);
                          const closing = openRouteKey === route._key;
                          if (!closing) {
                            const anchor = event.currentTarget.getBoundingClientRect();
                            const below = window.innerHeight - anchor.bottom - 12;
                            const above = anchor.top - 12;
                            const openDown = below >= 220 || below >= above;
                            setMenuPosition({
                              position: 'fixed',
                              left: anchor.left,
                              width: anchor.width,
                              minWidth: anchor.width,
                              maxWidth: anchor.width,
                              maxHeight: Math.min(300, openDown ? below : above),
                              ...(openDown
                                ? { top: anchor.bottom + 4 }
                                : { bottom: window.innerHeight - anchor.top + 4 }),
                            });
                          }
                          setOpenRouteKey(closing ? null : route._key);
                        }}
                        data-testid="settings-model-group-dialog-route-select"
                      >
                        <span>{label}</span>
                        <svg viewBox="0 0 20 20" fill="none" stroke="currentColor" strokeWidth={1.8} aria-hidden="true">
                          <path strokeLinecap="round" strokeLinejoin="round" d="M6 8l4 4 4-4" />
                        </svg>
                      </button>
                      {openRouteKey === route._key && menuPosition && (
                        <div className="settings-model-group-route-picker__menu" role="menu" style={menuPosition}>
                          {flatOptions.map((candidate) => (
                            <button
                              key={candidate.key}
                              type="button"
                              role="menuitemradio"
                              aria-checked={candidate.key === route.model_id}
                              disabled={disabled || isUnavailableModel(candidate.key, route.model_id)}
                              className="settings-model-group-route-picker__option"
                              onClick={() => replaceModel(route._key, candidate.key)}
                              data-testid="settings-model-group-dialog-model-option"
                              data-variant={candidate.key}
                            >
                              <span>{candidate.label}</span>
                              {candidate.key === route.model_id && <span aria-hidden>✓</span>}
                            </button>
                          ))}
                          {planGroups.map((planGroup) => {
                            const selectedInPlan = planGroup.options.some((candidate) => candidate.key === route.model_id);
                            return (
                              <div key={planGroup.key} className="settings-model-group-route-picker__plan">
                                <button
                                  type="button"
                                  role="menuitem"
                                  aria-haspopup="menu"
                                  aria-expanded={activePlanKey === planGroup.key}
                                  className="settings-model-group-route-picker__option"
                                  onMouseEnter={(event) => openPlanMenu(planGroup.key, event.currentTarget)}
                                  onFocus={(event) => openPlanMenu(planGroup.key, event.currentTarget)}
                                  onClick={(event) => openPlanMenu(planGroup.key, event.currentTarget)}
                                  data-testid="settings-model-group-dialog-plan-option"
                                  data-variant={planGroup.key}
                                >
                                  <span className="settings-model-group-route-picker__option-main">
                                    <span className="settings-model-group-route-picker__icon" aria-hidden="true">
                                      <ModelProviderIcon model={planGroup.options[0].model} />
                                    </span>
                                    <span>{planGroup.label}</span>
                                  </span>
                                  <span className="settings-model-group-route-picker__option-actions" aria-hidden>
                                    {selectedInPlan && <span>✓</span>}
                                    <span>›</span>
                                  </span>
                                </button>
                                {activePlanKey === planGroup.key && submenuPosition && createPortal(
                                  <div
                                    className="settings-model-group-route-picker__submenu"
                                    role="menu"
                                    style={{ position: 'fixed', top: submenuPosition.top, left: submenuPosition.left }}
                                    onMouseDown={(event) => event.preventDefault()}
                                  >
                                    {planGroup.options.map((candidate) => (
                                      <button
                                        key={candidate.key}
                                        type="button"
                                        role="menuitemradio"
                                        aria-checked={candidate.key === route.model_id}
                                        disabled={disabled || isUnavailableModel(candidate.key, route.model_id)}
                                        className="settings-model-group-route-picker__option"
                                        onClick={() => replaceModel(route._key, candidate.key)}
                                        data-testid="settings-model-group-dialog-plan-model-option"
                                        data-variant={candidate.key}
                                      >
                                        <span className="settings-model-group-route-picker__option-main">
                                          <span className="settings-model-group-route-picker__icon" aria-hidden="true">
                                            <ModelProviderIcon model={candidate.model} />
                                          </span>
                                          <span>{candidate.modelName}</span>
                                        </span>
                                        {candidate.key === route.model_id && <span aria-hidden>✓</span>}
                                      </button>
                                    ))}
                                  </div>,
                                  document.querySelector('.settings-model-group-dialog-surface') ?? document.body,
                                )}
                              </div>
                            );
                          })}
                        </div>
                      )}
                    </div>
                    <div className="settings-model-group-multiselect__route-actions">
                      <Button
                        size="sm"
                        variant="quiet"
                        disabled={disabled || index === 0}
                        onClick={() => onChange(moveRoute(value, route._key, 0))}
                        data-testid="settings-model-group-dialog-route-default"
                        data-variant={route.route_id || route._key}
                      >
                        {t('settingsPanel.models.modelGroupRouteDefault')}
                      </Button>
                      <Button
                        size="sm"
                        variant="quiet"
                        disabled={disabled}
                        onClick={() => onChange(value.filter((item) => item._key !== route._key))}
                        data-testid="settings-model-group-dialog-route-delete"
                        data-variant={route.route_id || route._key}
                      >
                        {t('common.delete')}
                      </Button>
                    </div>
                  </div>
                );
              })}
            </div>
          </div>
          <button
            type="button"
            className="settings-model-group-multiselect__add"
            disabled={disabled || !addableOption}
            onClick={() => {
              if (addableOption) onChange([...value, {
                _key: temporaryRouteKey(),
                model_id: addableOption.key,
                enabled: true,
              }]);
            }}
            data-testid="settings-model-group-dialog-add-route-btn"
          >
            {t('settingsPanel.models.addModel')}
          </button>
        </>
      )}
    </div>
  );
}

export function ModelGroupDialog({
  models,
  group,
  saving = false,
  onClose,
  onSave,
}: {
  models: ModelEntry[];
  group?: ModelGroup | null;
  saving?: boolean;
  onClose: () => void;
  onSave: (payload: ModelGroup) => Promise<void>;
}): JSX.Element {
  const { t } = useTranslation();
  const [saveError, setSaveError] = useState('');
  const [submitting, setSubmitting] = useState(false);
  const initialValues = useMemo<ModelGroupDraft>(
    () => ({
      name: group?.display_name?.trim() || '',
      routes: (group?.routes ?? []).map((route, index) => ({
        ...route,
        _key: route.route_id || `existing-route-${index}`,
      })),
    }),
    [group],
  );
  const form = useForm<ModelGroupDraft>({ initialValues });
  const closeBlocked = submitting || saving;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'model-group-dialog',
    form,
    closeBlocked,
    onClose,
  });

  const modelOptions = useMemo(
    () =>
      getGroupCandidateModels(models).map((model) => ({
        key: model.model_id,
        label: `${model.vendor_key?.trim() ? getVendorLabel(model.vendor_key, t) : t('settingsPanel.models.customVendor')}/${model.alias?.trim() || model.model_name}`,
        modelName: model.alias?.trim() || model.model_name,
        vendorKey: model.vendor_key?.trim() || undefined,
        plan: model.plan,
        model,
      })),
    [models, t],
  );

  const items = useMemo((): FormItem<ModelGroupDraft>[] => {
    return [
      {
        name: 'name',
        label: t('settingsPanel.models.modelGroupName'),
        component: 'input',
        required: true,
        placeholder: t('settingsPanel.models.modelGroupNamePlaceholder'),
      },
      {
        name: 'routes',
        label: t('settingsPanel.models.modelGroupModels'),
        component: 'custom',
        required: true,
        render: ({ id, value, error, disabled, onChange, onBlur }) => (
          <ModelRouteMultiSelect
            id={id}
            value={Array.isArray(value) ? value : []}
            disabled={disabled}
            invalid={Boolean(error)}
            options={modelOptions}
            emptyText={t('settingsPanel.models.selectConfiguredModelsEmpty')}
            onChange={(next) => onChange(next)}
            onBlur={onBlur}
          />
        ),
      },
    ];
  }, [modelOptions, t]);

  const rules = useMemo(
    () => ({
      name: [
        {
          trigger: 'blur' as const,
          validator: (value: unknown) =>
            typeof value === 'string' && value.trim().length > 0
              ? undefined
              : t('settingsPanel.validation.required'),
        },
      ],
      routes: [
        {
          trigger: ['change', 'blur'] as const,
          validator: (value: unknown) => {
            if (!Array.isArray(value) || value.length === 0) return t('settingsPanel.validation.required');
            return undefined;
          },
        },
      ],
    }),
    [t],
  );

  const confirm = async () => {
    const result = form.validate();
    if (!result.valid || closeBlocked) return;
    setSubmitting(true);
    setSaveError('');
    try {
      const routes = Array.isArray(result.values.routes)
        ? result.values.routes.map(({ _key, ...route }) => route)
        : [];
      await onSave(
        buildModelGroupUpsertPayload({
          displayName: String(result.values.name ?? ''),
          routes,
          enabled: group?.enabled !== false,
          isDefault: group?.is_default === true,
          requestConfig: group?.request_config ?? null,
          group,
        }),
      );
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
        className="settings-model-group-dialog"
        dialogClassName="settings-model-group-dialog-surface"
        title={t(group ? 'settingsPanel.models.editModelGroupTitle' : 'settingsPanel.models.addModelGroupTitle')}
        submitting={closeBlocked}
        confirmLabel={t(closeBlocked ? 'settingsPanel.models.savingModelGroup' : 'common.confirm')}
        cancelLabel={t('common.cancel')}
        testIdPrefix="settings-model-group-dialog"
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form
          className="settings-model-group-dialog__form"
          form={form}
          items={items}
          rules={rules}
          disabled={closeBlocked}
          optionalText={t('common.optional')}
          showOptional={false}
          testIdPrefix="settings-model-group-dialog"
        />
        {saveError ? (
          <div className="settings-page__error" role="alert" data-testid="settings-model-group-dialog-error">
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
