import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Switch } from '../../../../components/ui';
import { Form, FormDialog, useForm, useFormValue, type FormItem, type FormRules } from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import {
  DUPLEX_FORMATS,
  buildDuplexUpdates,
  createDuplexDraft,
  duplexFieldError,
  readDuplexSettings,
  type DuplexDraft,
  type DuplexField,
  type DuplexSettings,
} from './a2aDuplexConfig';

const FIELDS: readonly DuplexField[] = ['enabled', 'format', 'api_base', 'model', 'api_key'];
const prefix = 'settingsPanel.agent.a2aDuplex';

export function A2ADuplexSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, save } = useSettingsSource();
  const [editing, setEditing] = useState<DuplexDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const saved = readDuplexSettings(values);
  const blocked = disabled || !isConnected || busy;

  const run = async (draft: DuplexDraft) => {
    setBusy(true);
    setError('');
    try {
      await save(buildDuplexUpdates(draft), `${prefix}.title`);
      return true;
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const toggle = (next: boolean) => {
    const draft = { ...createDuplexDraft(saved), enabled: next };
    // Turning on without a complete config opens the editor instead of failing on save.
    if (next && FIELDS.some((field) => duplexFieldError(field, draft, saved))) setEditing(draft);
    else void run(draft);
  };

  const model = saved.model || (saved.format === 'jev' ? saved.jev.model : '');
  return (
    <>
      <SettingRow
        title={<span data-testid="settings-a2a-duplex-summary">{t(`${prefix}.title`)}</span>}
        description={
          <span data-testid="settings-a2a-duplex-status">
            {saved.enabled
              ? `${t(`${prefix}.formats.${saved.format}`)} · ${model || '-'}`
              : t(`${prefix}.description`)}
          </span>
        }
      >
        <Switch
          checked={saved.enabled}
          disabled={blocked}
          onChange={toggle}
          aria-label={t(`${prefix}.title`)}
          data-testid="settings-a2a-duplex-switch"
        />
        <Button
          disabled={blocked}
          onClick={() => setEditing(createDuplexDraft(saved))}
          data-testid="settings-a2a-duplex-edit-btn"
        >
          {t('settingsPanel.common.configure')}
        </Button>
      </SettingRow>
      {error && !editing ? (
        <div className="settings-page__error" role="alert" data-testid="settings-a2a-duplex-error">
          {error}
        </div>
      ) : null}
      {editing ? (
        <DuplexDialog
          initial={editing}
          saved={saved}
          onSave={run}
          error={error}
          busy={busy}
          onClose={() => {
            setEditing(null);
            setError('');
          }}
        />
      ) : null}
    </>
  );
}

function DuplexDialog({
  initial,
  saved,
  onSave,
  error,
  busy,
  onClose,
}: {
  initial: DuplexDraft;
  saved: DuplexSettings;
  onSave: (draft: DuplexDraft) => Promise<boolean>;
  error: string;
  busy: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const form = useForm<DuplexDraft>({ initialValues: initial });
  const enabled = useFormValue(form, 'enabled');
  const format = useFormValue(form, 'format');
  const jev = format === 'jev';
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'a2a-duplex-dialog',
    form,
    closeBlocked: busy,
    onClose,
  });

  const items = useMemo<FormItem<DuplexDraft>[]>(() => {
    const label = (field: DuplexField) => t(`${prefix}.fields.${field}`);
    const jevKeyState = t(saved.jev.keyConfigured ? 'settingsPanel.common.configured' : 'settingsPanel.common.notConfigured');
    return [
      {
        name: 'enabled',
        label: label('enabled'),
        component: 'switch',
        required: true,
        switchLabel: label('enabled'),
        description: t(`${prefix}.restartHint`),
      },
      {
        name: 'format',
        label: label('format'),
        component: 'select',
        required: true,
        options: DUPLEX_FORMATS.map((value) => ({ value, label: t(`${prefix}.formats.${value}`) })),
        description: t(`${prefix}.formatHints.${format}`),
      },
      {
        name: 'api_base',
        label: label('api_base'),
        component: 'input',
        required: enabled && !jev,
        placeholder: jev ? saved.jev.apiBase : 'https://',
        description: jev ? t(`${prefix}.jevFallbackHint`) : undefined,
      },
      {
        name: 'model',
        label: label('model'),
        component: 'input',
        required: enabled && !jev,
        placeholder: jev ? saved.jev.model : undefined,
      },
      {
        name: 'api_key',
        label: label('api_key'),
        component: 'input',
        type: 'password',
        required: enabled && !jev,
        description: jev ? `${jevKeyState} · ${t(`${prefix}.jevKeyHint`)}` : undefined,
        passwordVisibilityLabels: {
          show: t('settingsPanel.common.showValue'),
          hide: t('settingsPanel.common.hideValue'),
        },
      },
    ];
  }, [enabled, format, jev, saved, t]);
  const rules = useMemo<FormRules<DuplexDraft>>(
    () =>
      Object.fromEntries(
        FIELDS.map((field) => [
          field,
          [
            {
              validator: (_value: unknown, current: Readonly<DuplexDraft>) => {
                const problem = duplexFieldError(field, current, saved);
                return problem
                  ? t(problem === 'required' ? 'settingsPanel.validation.required' : `${prefix}.errors.${problem}`)
                  : undefined;
              },
            },
          ],
        ]),
      ),
    [saved, t],
  );

  const confirm = async () => {
    const result = form.validate();
    if (result.valid && (await onSave(result.values))) onClose();
  };

  return (
    <>
      <FormDialog
        open
        title={t(`${prefix}.title`)}
        submitting={busy}
        confirmDisabled={!isConnected}
        confirmLabel={t('common.save')}
        cancelLabel={t('common.cancel')}
        testIdPrefix="settings-a2a-duplex-dialog"
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form form={form} items={items} rules={rules} optionalText={t('common.optional')} />
        {error ? (
          <div className="settings-page__error" role="alert" data-testid="settings-a2a-duplex-dialog-error">
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
