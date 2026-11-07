import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, FieldError, Input } from '../../../../components/ui';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';
import { parseCollaborationVerifierInterval } from './multiModelCollaborationSettingsState';

export function CollaborationVerifierInterval({ disabled, onEditingChange }: SettingsCustomItemProps) {
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
