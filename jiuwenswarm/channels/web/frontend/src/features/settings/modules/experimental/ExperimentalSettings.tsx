import { useEffect, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Switch } from '../../../../components/ui';
import { Form, FormDialog, useForm } from '../../../../components/form';
import { setA2UIFeatureEnabled } from '../../../../features/a2ui/featureConfig';
import {
  EXTERNAL_CLI_AGENT_KINDS,
  ExternalCliAgentsSection,
  applyExternalCliAgentAtomicUpdates,
  externalCliKey,
  isCodexDependencyInstalling,
  type CodexDependencyInstallStatus,
  type ExternalCliConfigSaveResult,
} from '../../../../components/ExternalCliAgentsSection';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { parseConfigBoolean } from '../../services/settingsContract';
import { useSettingsFormDialogClose } from '../../services/useSettingsFormDialogClose';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { useUnsavedChanges } from '../../services/useUnsavedChanges';

const CLI_DEFAULTS: Record<string, string> = Object.fromEntries(
  EXTERNAL_CLI_AGENT_KINDS.flatMap((agent) => [
    [externalCliKey(agent, 'enabled'), 'false'],
    [externalCliKey(agent, 'use_builtin'), 'false'],
    [externalCliKey(agent, 'cli_path'), ''],
  ]),
);

function getInstallPhaseLabel(
  status: CodexDependencyInstallStatus | null,
  t: (key: string, options?: Record<string, string>) => string,
): string {
  const phase = status?.phase || status?.status || 'idle';
  const keys: Record<string, string> = {
    preparing: 'config.externalCli.dependencyInstallPhasePreparing',
    installing: 'config.externalCli.dependencyInstallPhaseInstalling',
    verifying: 'config.externalCli.dependencyInstallPhaseVerifying',
    succeeded: 'config.externalCli.dependencyInstallPhaseSucceeded',
    failed: 'config.externalCli.dependencyInstallPhaseFailed',
    running: 'config.externalCli.dependencyInstallPhaseInstalling',
  };
  return keys[phase] ? t(keys[phase], { agent: 'Codex' }) : phase;
}

function ProactiveLimitsDialog({
  values,
  onClose,
  onSave,
}: {
  values: { daily: string; rounds: string };
  onClose: () => void;
  onSave: (values: { daily: string; rounds: string }) => Promise<void>;
}) {
  const { t } = useTranslation();
  const form = useForm({ initialValues: values });
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const closeBlocked = saving;
  const { discardConfirmationOpen, requestClose, cancelDiscard, confirmDiscard } = useSettingsFormDialogClose({
    id: 'proactive-limits-dialog',
    form,
    closeBlocked,
    onClose,
  });
  const validator = (value: unknown) =>
    /^\d+$/.test(String(value)) && Number(value) >= 1 && Number(value) <= 50
      ? undefined
      : t('settingsPanel.validation.integerRange', { min: 1, max: 50 });
  const submit = async () => {
    const result = form.validate();
    if (!result.valid) return;
    setSaving(true);
    setSaveError('');
    try {
      await onSave(result.values);
      onClose();
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSaving(false);
    }
  };
  return (
    <>
      <FormDialog
        open
        title={t('settingsPanel.experimental.proactiveLimits')}
        submitting={closeBlocked}
        confirmLabel={t('common.confirm')}
        cancelLabel={t('common.cancel')}
        onConfirm={() => void submit()}
        onCancel={requestClose}
      >
        <Form
          form={form}
          optionalText={t('common.optional')}
          rules={{ daily: [{ validator }], rounds: [{ validator }] }}
          items={[
            {
              name: 'daily',
              label: t('settingsPanel.fields.proactive_recommendation_max_recommend_per_day.title'),
              component: 'input',
              type: 'number',
              required: true,
            },
            {
              name: 'rounds',
              label: t('settingsPanel.fields.proactive_recommendation_max_rounds_per_tick.title'),
              component: 'input',
              type: 'number',
              required: true,
            },
          ]}
        />
        {saveError ? (
          <div className="settings-page__error" role="alert">
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

function ExternalCliSettings({
  config,
  onConfigPatch,
  onSave,
  inheritedDisabled,
}: {
  config: Record<string, unknown>;
  onConfigPatch: (updates: Record<string, unknown>) => void;
  onSave: (values: Record<string, string>) => Promise<ExternalCliConfigSaveResult | void>;
  inheritedDisabled: boolean;
}) {
  const { t } = useTranslation();
  const { isConnected, onDetectExternalCli, onGetCodexDependencyInstallStatus, onSelectExternalCliPath } =
    useSettingsServices();
  const sourceValues = useMemo(
    () =>
      Object.fromEntries(Object.entries(CLI_DEFAULTS).map(([key, fallback]) => [key, String(config[key] ?? fallback)])),
    [config],
  );
  const [savedValues, setSavedValues] = useState(sourceValues);
  const [draftValues, setDraftValues] = useState(sourceValues);
  const [saving, setSaving] = useState(false);
  const [saveError, setSaveError] = useState('');
  const [codexInstallStatus, setCodexInstallStatus] = useState<CodexDependencyInstallStatus | null>(null);
  const changed = Object.keys(savedValues).some((key) => draftValues[key] !== savedValues[key]);
  const disabled = inheritedDisabled || saving || !isConnected;
  useUnsavedChanges('external-cli', changed);

  useEffect(() => {
    if (!changed) {
      setSavedValues(sourceValues);
      setDraftValues(sourceValues);
    }
  }, [changed, sourceValues]);

  useEffect(() => {
    if (!onGetCodexDependencyInstallStatus || codexInstallStatus?.status !== 'running') return undefined;
    let cancelled = false;
    const poll = async () => {
      try {
        const next = await onGetCodexDependencyInstallStatus();
        if (!cancelled) setCodexInstallStatus(next);
      } catch (error) {
        if (!cancelled) {
          setCodexInstallStatus((current) => ({
            ...(current ?? {}),
            status: 'failed',
            phase: 'failed',
            error: error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'),
          }));
        }
      }
    };
    const timer = window.setInterval(() => void poll(), 1500);
    void poll();
    return () => {
      cancelled = true;
      window.clearInterval(timer);
    };
  }, [codexInstallStatus?.status, onGetCodexDependencyInstallStatus, t]);

  useEffect(() => {
    if (codexInstallStatus?.status !== 'succeeded') return undefined;
    const timer = window.setTimeout(() => setCodexInstallStatus(null), 8000);
    return () => window.clearTimeout(timer);
  }, [codexInstallStatus?.status]);

  const submit = async () => {
    const updates: Record<string, string> = {};
    for (const agent of EXTERNAL_CLI_AGENT_KINDS) {
      applyExternalCliAgentAtomicUpdates(updates, agent, draftValues, savedValues);
    }
    if (!Object.keys(updates).length) return;
    setSaving(true);
    setSaveError('');
    try {
      const result = await onSave(updates);
      const nextValues = { ...draftValues };
      if (isCodexDependencyInstalling(result)) {
        nextValues[externalCliKey('codex', 'enabled')] = 'false';
        nextValues[externalCliKey('codex', 'use_builtin')] = 'false';
        nextValues[externalCliKey('codex', 'cli_path')] = '';
        setCodexInstallStatus(result?.codex_dependency_install ?? { status: 'running', phase: 'installing' });
      } else {
        setCodexInstallStatus(null);
      }
      setSavedValues(nextValues);
      setDraftValues(nextValues);
      onConfigPatch(nextValues);
    } catch (error) {
      setSaveError(error instanceof Error ? error.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  if (config.external_cli_agents_supported !== undefined && !parseConfigBoolean(config.external_cli_agents_supported))
    return null;
  const installLogs = codexInstallStatus?.log_tail?.filter((line) => line.trim()).slice(-4) ?? [];
  const showInstallStatus =
    codexInstallStatus && ['running', 'failed', 'succeeded'].includes(codexInstallStatus.status || '');

  return (
    <div className="settings-experimental-cli">
      {showInstallStatus ? (
        <div
          className={`settings-experimental-cli__status settings-experimental-cli__status--${codexInstallStatus?.status}`}
          role="status"
          aria-live="polite"
        >
          <strong>{getInstallPhaseLabel(codexInstallStatus, t)}</strong>
          <span>
            {codexInstallStatus?.status === 'succeeded'
              ? t('config.externalCli.dependencyInstalled', { agent: 'Codex' })
              : t('config.externalCli.dependencyInstalling', { agents: 'Codex' })}
          </span>
          {codexInstallStatus?.error ? <span>{codexInstallStatus.error}</span> : null}
          {codexInstallStatus?.status !== 'succeeded' && installLogs.length ? (
            <pre>{installLogs.join('\n')}</pre>
          ) : null}
        </div>
      ) : null}
      {saveError ? (
        <div className="settings-page__error" role="alert">
          {saveError}
        </div>
      ) : null}
      <ExternalCliAgentsSection
        draftValues={draftValues}
        onChange={(key, value) => {
          setDraftValues((current) => ({ ...current, [key]: value }));
          setSaveError('');
        }}
        onDetect={onDetectExternalCli}
        onSelectFile={onSelectExternalCliPath}
        t={t}
        disabled={disabled}
      />
      <div className="settings-experimental-cli__actions">
        <Button
          disabled={disabled || !changed}
          onClick={() => {
            setDraftValues(savedValues);
            setSaveError('');
          }}
        >
          {t('common.cancel')}
        </Button>
        <Button variant="primary" disabled={disabled || !changed} onClick={() => void submit()}>
          {t('common.save')}
        </Button>
      </div>
    </div>
  );
}

export function ExternalCliSettingsItem({ disabled }: SettingsCustomItemProps) {
  const source = useSettingsSource();
  return (
    <ExternalCliSettings
      config={source.values}
      inheritedDisabled={disabled}
      onConfigPatch={source.patchLocal}
      onSave={(updates) => source.save(updates, 'external-cli-agents') as Promise<ExternalCliConfigSaveResult | void>}
    />
  );
}

export function A2UISetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const a2ui = parseConfigBoolean(source.values.a2ui_enabled);
  async function updateA2UI(next: boolean): Promise<void> {
    await source.save({ a2ui_enabled: next }, 'a2ui-enabled');
    setA2UIFeatureEnabled(next);
  }

  return (
    <SettingRow
      title={t('settingsPanel.fields.a2ui_enabled.title')}
      description={t('settingsPanel.fields.a2ui_enabled.description')}
    >
      <Switch
        aria-label={t('settingsPanel.fields.a2ui_enabled.title')}
        checked={a2ui}
        disabled={disabled || !isConnected || source.savingKeys.has('a2ui_enabled')}
        onChange={(next) => void updateA2UI(next).catch(() => undefined)}
      />
    </SettingRow>
  );
}

export function ProactiveLimitsSetting({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const [limitsOpen, setLimitsOpen] = useState(false);
  const proactive = parseConfigBoolean(source.values.proactive_recommendation_enabled);
  return (
    <>
      <SettingRow
        title={t('settingsPanel.experimental.proactiveLimits')}
        description={t(
          proactive
            ? 'settingsPanel.experimental.proactiveLimitsDescription'
            : 'settingsPanel.experimental.proactiveLimitsDisabledDescription',
          {
            daily: String(source.values.proactive_recommendation_max_recommend_per_day ?? 10),
            rounds: String(source.values.proactive_recommendation_max_rounds_per_tick ?? 20),
          },
        )}
      >
        <Button disabled={disabled || !isConnected} onClick={() => setLimitsOpen(true)}>
          {t('common.modify')}
        </Button>
      </SettingRow>
      {limitsOpen ? (
        <ProactiveLimitsDialog
          values={{
            daily: String(source.values.proactive_recommendation_max_recommend_per_day ?? 10),
            rounds: String(source.values.proactive_recommendation_max_rounds_per_tick ?? 20),
          }}
          onClose={() => setLimitsOpen(false)}
          onSave={async (values) => {
            await source.save(
              {
                proactive_recommendation_max_recommend_per_day: values.daily,
                proactive_recommendation_max_rounds_per_tick: values.rounds,
              },
              'proactive-limits',
            );
          }}
        />
      ) : null}
    </>
  );
}
