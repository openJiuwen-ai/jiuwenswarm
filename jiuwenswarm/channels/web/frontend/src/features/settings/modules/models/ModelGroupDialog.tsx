import { useMemo, useState } from 'react';
import { GripVertical } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ModelEntry, ModelGroup, ModelRoute } from '../../../../types';
import { Button } from '../../../../components/ui';
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
  options: readonly { key: string; label: string }[];
  emptyText: string;
  onChange: (next: RouteDraft[]) => void;
  onBlur: () => void;
}): JSX.Element {
  const { t } = useTranslation();
  const [draggingId, setDraggingId] = useState<string | null>(null);
  const optionByKey = new Map(options.map((option) => [option.key, option]));
  const addableOption = options[0];

  const replaceModel = (routeKey: string, nextId: string) => {
    if (!nextId) return;
    onChange(value.map((route) => route._key === routeKey ? { ...route, model_id: nextId } : route));
  };

  return (
    <div
      id={id}
      className={`settings-model-group-multiselect${invalid ? ' settings-model-group-multiselect--invalid' : ''}`}
      role="group"
      aria-disabled={disabled || undefined}
      data-testid="settings-model-group-dialog-model-multiselect"
      onBlur={(event) => {
        if (!event.currentTarget.contains(event.relatedTarget as Node | null)) onBlur();
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
                    <select
                      className="settings-model-group-multiselect__route-select"
                      aria-label={t('settingsPanel.models.modelGroupTableModel')}
                      value={route.model_id}
                      disabled={disabled}
                      title={label}
                      onChange={(event) => replaceModel(route._key, event.target.value)}
                    >
                      {options.map((candidate) => (
                        <option
                          key={candidate.key}
                          value={candidate.key}
                        >
                          {candidate.label}
                        </option>
                      ))}
                    </select>
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
          validator: (value: unknown) =>
            Array.isArray(value) && value.length > 0 ? undefined : t('settingsPanel.validation.required'),
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
      await onSave(
        buildModelGroupUpsertPayload({
          displayName: String(result.values.name ?? ''),
          routes: Array.isArray(result.values.routes)
            ? result.values.routes.map(({ _key, ...route }) => route)
            : [],
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
