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
  DUPLEX_JEV_DEFAULT_BASE,
  DUPLEX_JEV_PROVIDERS,
  buildDuplexClearUpdates,
  buildDuplexUpdates,
  createDuplexDraft,
  duplexFieldError,
  isDuplexConfigured,
  jevSharedKey,
  readDuplexSettings,
  withJevDefaults,
  type DuplexDraft,
  type DuplexField,
  type DuplexSettings,
} from './a2aDuplexConfig';

const FIELDS: readonly DuplexField[] = [
  'enabled',
  'mode',
  'format',
  'api_base',
  'endpoint',
  'model',
  'api_key',
  'timeout_seconds',
];
const prefix = 'settingsPanel.agent.a2aDuplex';

export function A2ADuplexSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, save, patchLocal } = useSettingsSource();
  const [editing, setEditing] = useState<DuplexDraft | null>(null);
  const [clearing, setClearing] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const saved = readDuplexSettings(values);
  const blocked = disabled || !isConnected || busy;

  const run = async (updates: Record<string, string>, operation: string) => {
    setBusy(true);
    setError('');
    try {
      await save(updates, operation);
      // config.get never echoes the key; keep only whether one is stored.
      if ('duplex_router_api_key' in updates)
        patchLocal({
          duplex_router_api_key: '',
          duplex_router_api_key_configured: updates.duplex_router_api_key ? 'true' : 'false',
        });
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
    else void run(buildDuplexUpdates(draft), `${prefix}.title`);
  };

  const status = saved.enabled ? t(`${prefix}.modes.${saved.mode}`) : t(`${prefix}.disabled`);
  return (
    <>
      <SettingRow
        title={<span data-testid="settings-a2a-duplex-summary">{t(`${prefix}.title`)}</span>}
        description={
          <span data-testid="settings-a2a-duplex-status">
            {isDuplexConfigured(saved)
              ? `${status} · ${t(`${prefix}.formats.${saved.format}`)} · ${saved.model || '-'}`
              : t('settingsPanel.common.notConfigured')}
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
        {isDuplexConfigured(saved) ? (
          <Button
            disabled={blocked}
            onClick={() => {
              setError('');
              setClearing(true);
            }}
            data-testid="settings-a2a-duplex-clear-btn"
          >
            {t('settingsPanel.common.clear')}
          </Button>
        ) : null}
      </SettingRow>
      {error && !editing && !clearing ? (
        <div className="settings-page__error" role="alert" data-testid="settings-a2a-duplex-error">
          {error}
        </div>
      ) : null}
      {editing ? (
        <DuplexDialog
          initial={editing}
          saved={saved}
          onSave={(draft) => run(buildDuplexUpdates(draft), `${prefix}.title`)}
          error={error}
          busy={busy}
          onClose={() => {
            setEditing(null);
            setError('');
          }}
        />
      ) : null}
      <SettingsConfirmDialog
        open={clearing}
        title={t(`${prefix}.clearTitle`)}
        message={t(`${prefix}.clearConfirm`)}
        confirming={busy}
        error={error || undefined}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() =>
          void run(buildDuplexClearUpdates(), `${prefix}.clearTitle`).then((ok) => ok && setClearing(false))
        }
        onCancel={() => {
          if (!busy) setClearing(false);
        }}
      />
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
  const apiBase = useFormValue(form, 'api_base');
  const shared = jevSharedKey({ format, api_base: apiBase });
  const jevModels: readonly string[] = DUPLEX_JEV_PROVIDERS.map((name) => saved.jev[name].model);
  // Jev: prefill url/endpoint/model from the url host; leaving Jev drops a Jev default model.
  const syncDefaults = (values: Readonly<DuplexDraft>, formatChanged: boolean) => {
    if (values.format === 'jev') {
      const next = withJevDefaults(values, saved);
      // Only a format switch fills the url, so the user can still clear it.
      form.setValues(formatChanged ? next : { endpoint: next.endpoint, model: next.model });
    } else if (jevModels.includes(values.model.trim())) form.setFieldValue('model', '');
  };
  const keyHint = shared
    ? `${t(saved.jev[shared].keyConfigured ? 'settingsPanel.common.configured' : 'settingsPanel.common.notConfigured')} · ${t(`${prefix}.jevKeyHint`)}`
    : undefined;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'a2a-duplex-dialog',
    form,
    closeBlocked: busy,
    onClose,
  });

  const label = (field: DuplexField) => t(`${prefix}.fields.${field}`);
  const items = useMemo<FormItem<DuplexDraft>[]>(
    () => [
      {
        name: 'enabled',
        label: label('enabled'),
        component: 'switch',
        switchLabel: label('enabled'),
        description: t(`${prefix}.restartHint`),
      },
      {
        name: 'format',
        label: label('format'),
        component: 'select',
        options: DUPLEX_FORMATS.map((value) => ({ value, label: t(`${prefix}.formats.${value}`) })),
        description: t(`${prefix}.formatHints.${format}`),
        onChange: (_next, values) => syncDefaults(values, true),
      },
      {
        name: 'api_base',
        label: label('api_base'),
        component: 'input',
        required: enabled && format !== 'jev',
        placeholder: format === 'jev' ? DUPLEX_JEV_DEFAULT_BASE : 'https://',
        onChange: (_next, values) => syncDefaults(values, false),
      },
      ...(format === 'jev'
        ? ([
            {
              name: 'endpoint',
              label: label('endpoint'),
              component: 'input',
              description: t(`${prefix}.endpointHint`),
            },
          ] satisfies FormItem<DuplexDraft>[])
        : []),
      { name: 'model', label: label('model'), component: 'input', required: enabled && !shared },
      {
        name: 'api_key',
        label: label('api_key'),
        component: 'input',
        type: 'password',
        required: enabled && !shared && !saved.hasApiKey,
        description: keyHint,
        placeholder: saved.hasApiKey && (!shared || saved.apiKeyEnv) ? t(`${prefix}.keyKept`) : undefined,
        passwordVisibilityLabels: {
          show: t('settingsPanel.common.showValue'),
          hide: t('settingsPanel.common.hideValue'),
        },
      },
      {
        name: 'timeout_seconds',
        label: label('timeout_seconds'),
        component: 'input',
        type: 'number',
        min: 0.1,
        max: 60,
        step: 0.5,
      },
    ],
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [enabled, format, shared, saved, t],
  );
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
