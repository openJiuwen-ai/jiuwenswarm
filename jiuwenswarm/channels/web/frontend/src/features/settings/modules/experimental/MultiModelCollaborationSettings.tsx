import { useEffect, useRef, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { Button, FieldError, Input, Switch } from '../../../../components/ui';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';
import {
  buildCollaborationJevUpdates,
  createCollaborationJevDraft,
  parseCollaborationVerifierInterval,
  validateCollaborationJevDraft,
  type CollaborationJevDraft,
} from './multiModelCollaborationSettingsState';

function CollaborationJevDialog({
  config,
  keyConfigured,
  enableAfterSave,
  onSave,
  onClose,
}: {
  config: Readonly<Record<string, unknown>>;
  keyConfigured: boolean;
  enableAfterSave: boolean;
  onSave: (updates: Record<string, string>) => Promise<void>;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const form = useForm<CollaborationJevDraft>({ initialValues: createCollaborationJevDraft(config) });
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const busy = useRef(false);
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'collaboration-jev-dialog',
    form,
    closeBlocked: saving,
    onClose,
  });
  const submit = async () => {
    if (busy.current || !isConnected) return;
    const result = form.validate();
    if (!result.valid) return;
    busy.current = true;
    setSaving(true);
    setError('');
    try {
      await onSave(buildCollaborationJevUpdates(result.values, enableAfterSave));
      // Clear newly entered secrets before dismissing the dialog.
      form.reset({ ...result.values, apiKey: '' });
      onClose();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      busy.current = false;
      setSaving(false);
    }
  };
  return (
    <>
      <FormDialog
        open
        title={t('settingsPanel.multiModelCollaboration.jevConfigTitle')}
        description={t('settingsPanel.multiModelCollaboration.jevConfigDescription')}
        submitting={saving}
        confirmDisabled={!isConnected}
        confirmLabel={t('common.save')}
        cancelLabel={t('common.cancel')}
        testIdPrefix="settings-collaboration-jev-dialog"
        onConfirm={() => void submit()}
        onCancel={requestClose}
      >
        <Form
          form={form}
          optionalText={t('common.optional')}
          disabled={saving || !isConnected}
          testIdPrefix="settings-collaboration-jev-form"
          rules={Object.fromEntries(
            (['apiBase', 'apiKey'] as const).map((field) => [
              field,
              [
                {
                  validator: (_value: unknown, values: Readonly<CollaborationJevDraft>) => {
                    const errorKey = validateCollaborationJevDraft(values, keyConfigured)[field];
                    return errorKey ? t(errorKey) : undefined;
                  },
                },
              ],
            ]),
          )}
          items={[
            {
              name: 'apiBase',
              component: 'input',
              label: t('settingsPanel.fields.moa_jev_api_base.title'),
              required: true,
            },
            {
              name: 'apiKey',
              component: 'input',
              type: 'password',
              label: t('settingsPanel.fields.moa_jev_api_key.title'),
              required: !keyConfigured,
              placeholder: t(keyConfigured ? 'settingsPanel.multiModelCollaboration.keepKey' : 'config.enterValue'),
              description: keyConfigured ? t('settingsPanel.multiModelCollaboration.keepKey') : undefined,
              passwordVisibilityLabels: {
                show: t('settingsPanel.common.showValue'),
                hide: t('settingsPanel.common.hideValue'),
              },
            },
          ]}
        />
        {error ? (
          <div className="settings-page__error" role="alert" data-testid="settings-collaboration-jev-dialog-error">
            {error}
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

function CollaborationVerifierInterval({ disabled, onEditingChange }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const value = String(source.values.moa_verifier_interval ?? '3');
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(value);
  const [error, setError] = useState('');
  const saving = source.savingKeys.has('moa_verifier_interval');
  const validInterval = parseCollaborationVerifierInterval(draft);
  const changed = editing && draft.trim() !== value;
  useUnsavedChanges('collaboration-verifier-interval', changed);
  useEffect(() => {
    onEditingChange?.(editing);
    return () => onEditingChange?.(false);
  }, [editing, onEditingChange]);
  useEffect(() => {
    if (!editing) setDraft(value);
  }, [editing, value]);
  const cancel = () => {
    setDraft(value);
    setError('');
    setEditing(false);
  };
  const save = async () => {
    if (validInterval === undefined || saving) return;
    setError('');
    try {
      await source.save({ moa_verifier_interval: validInterval }, 'moa-verifier-interval');
      setEditing(false);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    }
  };
  const controlDisabled = disabled || !isConnected || saving;
  const invalid = editing && validInterval === undefined;
  return (
    <SettingRow
      title={
        <span data-testid="settings-collaboration-interval-title">
          {t('settingsPanel.fields.moa_verifier_interval.title')}
        </span>
      }
      description={
        <span data-testid="settings-collaboration-interval-description">
          {t('settingsPanel.fields.moa_verifier_interval.description')}
        </span>
      }
      subSettings={
        invalid || error ? (
          <div className="settings-page__error" data-testid="settings-collaboration-interval-error">
            <FieldError>
              {invalid ? t('settingsPanel.multiModelCollaboration.errors.intervalInvalid') : error}
            </FieldError>
          </div>
        ) : undefined
      }
    >
      <div className="settings-inline-input" data-testid="settings-collaboration-interval">
        <Input
          type="number"
          min={1}
          step={1}
          aria-label={t('settingsPanel.fields.moa_verifier_interval.title')}
          value={editing ? draft : value}
          readOnly={!editing}
          disabled={controlDisabled}
          invalid={invalid}
          onChange={setDraft}
          onKeyDown={(event) => {
            if (!editing) return;
            if (event.key === 'Enter' && changed && validInterval !== undefined) void save();
            if (event.key === 'Escape') cancel();
          }}
          data-testid="settings-collaboration-interval-input"
        />
        {editing ? (
          <div className="settings-inline-input__actions">
            <Button
              size="sm"
              disabled={saving}
              onClick={cancel}
              data-testid="settings-collaboration-interval-cancel-btn"
            >
              {t('common.cancel')}
            </Button>
            <Button
              size="sm"
              variant="primary"
              disabled={controlDisabled || !changed || validInterval === undefined}
              onClick={() => void save()}
              data-testid="settings-collaboration-interval-save-btn"
            >
              {t('common.save')}
            </Button>
          </div>
        ) : (
          <Button
            size="sm"
            disabled={controlDisabled}
            onClick={() => setEditing(true)}
            data-testid="settings-collaboration-interval-modify-btn"
          >
            {t('common.modify')}
          </Button>
        )}
      </div>
    </SettingRow>
  );
}

export function MultiModelVerificationSettings({ disabled, onEditingChange }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, save: saveConfig } = useSettingsSource();
  const [dialog, setDialog] = useState<{ enableAfterSave: boolean } | null>(null);
  const [clearing, setClearing] = useState(false);
  const [intervalEditing, setIntervalEditing] = useState(false);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const busy = useRef(false);
  const keyConfigured = parseConfigBoolean(values.moa_jev_api_key_configured);
  const jevEnabled = parseConfigBoolean(values.moa_jev_enabled);
  const controlDisabled = disabled || !isConnected || saving;
  const editing = Boolean(dialog) || clearing || intervalEditing || saving;
  useEffect(() => {
    onEditingChange?.(editing);
    return () => onEditingChange?.(false);
  }, [editing, onEditingChange]);
  const save = async (updates: Record<string, string>) => {
    if (busy.current) return;
    busy.current = true;
    setSaving(true);
    setError('');
    try {
      await saveConfig(updates, 'multi-model-collaboration-jev', { optimistic: false });
    } finally {
      busy.current = false;
      setSaving(false);
    }
  };
  const toggle = async (enabled: boolean) => {
    if (enabled && !keyConfigured) {
      setDialog({ enableAfterSave: true });
      return;
    }
    try {
      await save({ moa_jev_enabled: enabled ? 'true' : 'false' });
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    }
  };
  const clear = async () => {
    try {
      await save({ moa_jev_api_key_clear: 'true', moa_jev_enabled: 'false' });
      setClearing(false);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    }
  };
  return (
    <>
      <SettingRow
        title={
          <span data-testid="settings-collaboration-jev-title">{t('settingsPanel.fields.moa_jev_enabled.title')}</span>
        }
        description={
          <span data-testid="settings-collaboration-jev-description">
            {t('settingsPanel.fields.moa_jev_enabled.description')}
          </span>
        }
        meta={
          error && !clearing ? (
            <span role="alert" data-testid="settings-collaboration-jev-error">
              {error}
            </span>
          ) : undefined
        }
        subSettings={
          <SettingRow
            title={
              <span data-testid="settings-collaboration-jev-config-title">
                {t('settingsPanel.multiModelCollaboration.jevConfigTitle')}
              </span>
            }
            description={
              <span
                data-testid="settings-collaboration-jev-config-status"
                data-variant={keyConfigured ? 'configured' : 'not-configured'}
              >
                {keyConfigured ? t('settingsPanel.common.configured') : t('settingsPanel.common.notConfigured')}
              </span>
            }
          >
            <Button
              disabled={controlDisabled}
              onClick={() => setDialog({ enableAfterSave: false })}
              data-testid="settings-collaboration-jev-configure-btn"
            >
              {t('settingsPanel.common.configure')}
            </Button>
            {keyConfigured ? (
              <Button
                disabled={controlDisabled}
                onClick={() => {
                  setError('');
                  setClearing(true);
                }}
                data-testid="settings-collaboration-jev-clear-btn"
              >
                {t('settingsPanel.common.clear')}
              </Button>
            ) : null}
          </SettingRow>
        }
      >
        <Switch
          aria-label={t('settingsPanel.fields.moa_jev_enabled.title')}
          checked={jevEnabled}
          disabled={controlDisabled}
          onChange={(enabled) => void toggle(enabled)}
          data-testid="settings-collaboration-jev-switch"
        />
      </SettingRow>
      <CollaborationVerifierInterval disabled={disabled} onEditingChange={setIntervalEditing} />
      {dialog ? (
        <CollaborationJevDialog
          config={values}
          keyConfigured={keyConfigured}
          enableAfterSave={dialog.enableAfterSave}
          onSave={save}
          onClose={() => setDialog(null)}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t('settingsPanel.dialog.clearTitle', { name: t('settingsPanel.fields.moa_jev_api_key.title') })}
        message={t('settingsPanel.multiModelCollaboration.clearKeyConfirm')}
        confirming={saving}
        error={error || undefined}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void clear()}
        onCancel={() => {
          if (!saving) setClearing(false);
        }}
      />
    </>
  );
}
