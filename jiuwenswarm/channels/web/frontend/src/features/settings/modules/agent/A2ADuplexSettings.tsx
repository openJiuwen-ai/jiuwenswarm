import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select, Switch } from '../../../../components/ui';
import { FormDialog } from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';

const DEFAULT_BASES: Record<string, string> = {
  sdk: '',
  jev: 'https://api.typesafe.ai/v1',
  mindshub: 'https://api.mindshub.ai/v1',
  clef: 'https://api.cloudflare.com/client/v4',
};
const DEFAULT_MODELS: Record<string, string> = { sdk: '', jev: 'jev-1.13.0', mindshub: 'mindshub_air', clef: 'clef' };
type Draft = Record<string, string>;

export function A2ADuplexSettings({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected } = useSettingsServices();
  const source = useSettingsSource();
  const [draft, setDraft] = useState<Draft | null>(null);
  const [advanced, setAdvanced] = useState(false);
  const [clearOpen, setClearOpen] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState('');
  const value = (field: string) => String(source.values[`duplex_router_${field}`] ?? '');
  const providers = (source.values.duplex_router_backends ?? {}) as Record<
    string,
    {
      api_base?: string;
      interrupt_threshold?: number;
    }
  >;
  const backend = value('backend') || 'sdk';
  const model = backend === 'clef' ? value('model') || 'clef' : value('model_name') || DEFAULT_MODELS[backend];
  const blocked = disabled || !isConnected || busy;

  const openEditor = () => {
    setError('');
    setAdvanced(false);
    setDraft({
      backend,
      mode: value('mode') || 'off',
      model_name: model,
      api_base: value('api_base'),
      api_key: '',
      account_id: value('account_id'),
      timeout_seconds: value('timeout_seconds') || '2',
      interrupt_threshold: value('interrupt_threshold') || '0.9',
      endpoint_path: value('endpoint_path') || 'systemone',
    });
  };
  const update = (field: string, next: string) => setDraft((current) => current && { ...current, [field]: next });
  const selectBackend = (next: string) =>
    setDraft(
      (current) =>
        current && {
          ...current,
          backend: next,
          api_base: next === backend ? value('api_base') : providers[next]?.api_base || DEFAULT_BASES[next],
          model_name: next === backend ? model : DEFAULT_MODELS[next],
          interrupt_threshold:
            next === backend
              ? value('interrupt_threshold') || '0.9'
              : String(providers[next]?.interrupt_threshold ?? 0.9),
        },
    );

  const saveDraft = async () => {
    if (!draft) return;
    if (draft.mode === 'active' && draft.backend === 'sdk' && !draft.model_name.trim()) {
      setError(t('settingsPanel.validation.required'));
      return;
    }
    setBusy(true);
    setError('');
    const updates: Record<string, string> = {
      duplex_router_mode: draft.mode,
      duplex_router_backend: draft.backend,
      duplex_router_timeout_seconds: draft.timeout_seconds,
      [draft.backend === 'clef' ? 'duplex_router_model' : 'duplex_router_model_name']: draft.model_name.trim(),
    };
    if (draft.backend !== 'sdk') {
      updates.duplex_router_api_base = draft.api_base.trim() || DEFAULT_BASES[draft.backend];
      if (draft.api_key.trim()) updates.duplex_router_api_key = draft.api_key.trim();
    }
    if (draft.backend === 'jev' || draft.backend === 'clef') {
      updates.duplex_router_interrupt_threshold = draft.interrupt_threshold;
    }
    if (draft.backend === 'jev') updates.duplex_router_endpoint_path = draft.endpoint_path;
    if (draft.backend === 'clef') updates.duplex_router_account_id = draft.account_id.trim();
    try {
      await source.save(updates, 'a2a-duplex-router');
      const apiBase = draft.backend === 'sdk' ? '' : draft.api_base.trim() || DEFAULT_BASES[draft.backend];
      source.patchLocal({
        duplex_router_api_base: apiBase,
        duplex_router_backends: {
          ...providers,
          ...(draft.backend === 'sdk'
            ? {}
            : {
                [draft.backend]: { api_base: apiBase, interrupt_threshold: Number(draft.interrupt_threshold) },
              }),
        },
      });
      setDraft(null);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setBusy(false);
    }
  };

  const fieldTitle = (field: string) => t(`settingsPanel.fields.duplex_router_${field}.title`);
  const clear = async () => {
    setBusy(true);
    setError('');
    const updates: Record<string, string> = {
      duplex_router_mode: 'off',
      duplex_router_model_name: '',
      duplex_router_api_key: '',
    };
    if (backend !== 'sdk') updates.duplex_router_api_base = DEFAULT_BASES[backend];
    if (backend === 'clef') {
      updates.duplex_router_model = 'clef';
      updates.duplex_router_account_id = '';
    }
    try {
      await source.save(updates, 'a2a-duplex-router-clear');
      source.patchLocal({
        duplex_router_backends: {
          ...providers,
          [backend]: { ...providers[backend], api_base: DEFAULT_BASES[backend] },
        },
      });
      setClearOpen(false);
    } catch (clearError) {
      setError(clearError instanceof Error ? clearError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setBusy(false);
    }
  };
  const input = (field: string, inputDisabled = false) => (
    <SettingRow
      title={
        <span data-testid="settings-a2a-field-title" data-variant={field}>
          {fieldTitle(field)}
        </span>
      }
    >
      <Input
        aria-label={fieldTitle(field)}
        type={field === 'api_key' ? 'password' : 'text'}
        value={draft?.[field] ?? ''}
        disabled={busy || inputDisabled}
        placeholder={field === 'api_key' && value('api_key') ? '••••••••' : undefined}
        onChange={(next) => update(field, next)}
        autoComplete="off"
        data-testid="settings-a2a-field-input"
        data-variant={field}
      />
    </SettingRow>
  );
  const select = (field: string, options: string[]) => (
    <SettingRow
      title={
        <span data-testid="settings-a2a-field-title" data-variant={field}>
          {fieldTitle(field)}
        </span>
      }
    >
      <Select
        aria-label={fieldTitle(field)}
        value={draft?.[field] ?? ''}
        options={options.map((item) => ({ value: item, label: item }))}
        onChange={(next) => (field === 'backend' ? selectBackend(next) : update(field, next))}
        disabled={busy}
        data-testid="settings-a2a-field-select"
        data-variant={field}
      />
    </SettingRow>
  );

  return (
    <>
      <SettingRow
        title={
          <span data-testid="settings-a2a-model-summary">{model || t('settingsPanel.agent.a2aNotConfigured')}</span>
        }
        description={
          <span data-testid="settings-a2a-status">
            {backend} ·{' '}
            {t(
              value('mode') === 'active'
                ? 'settingsPanel.options.duplexRouterModeActive'
                : 'settingsPanel.options.duplexRouterModeOff',
            )}
          </span>
        }
      >
        <Button variant="quiet" size="sm" disabled={blocked} onClick={openEditor} data-testid="settings-a2a-edit-btn">
          {t('common.modify')}
        </Button>
        <Button
          variant="quiet"
          size="sm"
          disabled={blocked}
          onClick={() => {
            setError('');
            setClearOpen(true);
          }}
          data-testid="settings-a2a-clear-btn"
        >
          {t('settingsPanel.common.clear')}
        </Button>
      </SettingRow>
      {draft ? (
        <FormDialog
          open
          title={t('settingsPanel.agent.a2aDuplexRouter')}
          submitting={busy}
          confirmDisabled={disabled || !isConnected}
          confirmLabel={t('common.save')}
          cancelLabel={t('common.cancel')}
          testIdPrefix="settings-a2a-config-dialog"
          onConfirm={() => void saveDraft()}
          onCancel={() => {
            if (!busy) setDraft(null);
          }}
        >
          <SettingRow title={<span data-testid="settings-a2a-mode-title">{fieldTitle('mode')}</span>}>
            <Switch
              aria-label={fieldTitle('mode')}
              checked={draft.mode === 'active'}
              onChange={(checked) => update('mode', checked ? 'active' : 'off')}
              disabled={busy}
              data-testid="settings-a2a-mode-switch"
            />
          </SettingRow>
          {draft.backend === 'clef' ? select('model_name', ['clef', 'clef-flash']) : input('model_name')}
          {input('api_base', draft.backend === 'sdk')}
          {input('api_key', draft.backend === 'sdk')}
          <p data-testid="settings-a2a-defaults-hint">{t('settingsPanel.agent.a2aDefaultsHint')}</p>
          <Button
            variant="quiet"
            aria-expanded={advanced}
            onClick={() => setAdvanced(!advanced)}
            data-testid="settings-a2a-advanced-toggle"
          >
            {t('settingsPanel.agent.a2aAdvanced')}
          </Button>
          {advanced ? (
            <div data-testid="settings-a2a-advanced-fields">
              {select('backend', ['sdk', 'jev', 'mindshub', 'clef'])}
              {input('timeout_seconds')}
              {draft.backend === 'jev' || draft.backend === 'clef' ? input('interrupt_threshold') : null}
              {draft.backend === 'jev' ? select('endpoint_path', ['systemone', 'decisions']) : null}
              {draft.backend === 'clef' ? input('account_id') : null}
            </div>
          ) : null}
          {error ? (
            <div className="settings-page__error" role="alert" data-testid="settings-a2a-error">
              {error}
            </div>
          ) : null}
        </FormDialog>
      ) : null}
      <SettingsConfirmDialog
        open={clearOpen}
        title={t('settingsPanel.agent.a2aClearTitle')}
        message={t('settingsPanel.agent.a2aClearConfirm')}
        confirming={busy}
        error={error}
        confirmLabel={t('settingsPanel.common.clear')}
        confirmVariant="danger"
        onConfirm={() => void clear()}
        onCancel={() => {
          if (!busy) setClearOpen(false);
        }}
      />
    </>
  );
}
