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
  TEAM_RULES_FORMATS,
  buildTeamRulesUpdates,
  createTeamRulesDraft,
  teamRulesFieldError,
  readTeamRulesSettings,
  type TeamRulesDraft,
  type TeamRulesField,
  type TeamRulesSaved,
} from './teamRulesConfig';

const FIELDS: readonly TeamRulesField[] = ['enabled', 'format', 'api_base', 'model', 'api_key'];
const prefix = 'settingsPanel.agent.teamRules';

export function TeamRulesSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const { values, save } = useSettingsSource();
  const [editing, setEditing] = useState<TeamRulesDraft | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const saved = readTeamRulesSettings(values);
  const blocked = disabled || !isConnected || busy;

  const run = async (draft: TeamRulesDraft) => {
    setBusy(true);
    setError('');
    try {
      await save(buildTeamRulesUpdates(draft), `${prefix}.title`);
      return true;
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
      return false;
    } finally {
      setBusy(false);
    }
  };

  const toggle = (next: boolean) => {
    const draft = { ...createTeamRulesDraft(saved), enabled: next };
    // Turning on without a complete config opens the editor instead of failing on save.
    if (next && FIELDS.some((field) => teamRulesFieldError(field, draft, saved))) setEditing(draft);
    else void run(draft);
  };

  const model = saved.model || (saved.format === 'jev' ? saved.jev.model : '');
  return (
    <>
      <SettingRow
        title={<span data-testid="settings-team-rules-summary">{t(`${prefix}.title`)}</span>}
        description={
          <span data-testid="settings-team-rules-status">
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
          data-testid="settings-team-rules-switch"
        />
        <Button
          disabled={blocked}
          onClick={() => setEditing(createTeamRulesDraft(saved))}
          data-testid="settings-team-rules-edit-btn"
        >
          {t('settingsPanel.common.configure')}
        </Button>
      </SettingRow>
      {error && !editing ? (
        <div className="settings-page__error" role="alert" data-testid="settings-team-rules-error">
          {error}
        </div>
      ) : null}
      {editing ? (
        <TeamRulesDialog
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

function TeamRulesDialog({
  initial,
  saved,
  onSave,
  error,
  busy,
  onClose,
}: {
  initial: TeamRulesDraft;
  saved: TeamRulesSaved;
  onSave: (draft: TeamRulesDraft) => Promise<boolean>;
  error: string;
  busy: boolean;
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const form = useForm<TeamRulesDraft>({ initialValues: initial });
  const enabled = useFormValue(form, 'enabled');
  const format = useFormValue(form, 'format');
  const jev = format === 'jev';
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'team-rules-dialog',
    form,
    closeBlocked: busy,
    onClose,
  });

  const items = useMemo<FormItem<TeamRulesDraft>[]>(() => {
    const label = (field: TeamRulesField) => t(`${prefix}.fields.${field}`);
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
        options: TEAM_RULES_FORMATS.map((value) => ({ value, label: t(`${prefix}.formats.${value}`) })),
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
  const rules = useMemo<FormRules<TeamRulesDraft>>(
    () =>
      Object.fromEntries(
        FIELDS.map((field) => [
          field,
          [
            {
              validator: (_value: unknown, current: Readonly<TeamRulesDraft>) => {
                const problem = teamRulesFieldError(field, current, saved);
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
        testIdPrefix="settings-team-rules-dialog"
        onConfirm={() => void confirm()}
        onCancel={requestClose}
      >
        <Form form={form} items={items} rules={rules} optionalText={t('common.optional')} />
        {error ? (
          <div className="settings-page__error" role="alert" data-testid="settings-team-rules-dialog-error">
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
