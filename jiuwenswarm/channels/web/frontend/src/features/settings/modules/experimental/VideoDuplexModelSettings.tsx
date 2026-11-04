import { useEffect, useState } from 'react';
import { CheckCircle2, LoaderCircle } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import qwenLogo from '../../../../assets/providers/qwen.svg';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button, Select, Switch } from '../../../../components/ui';
import { FormDialog } from '../../../../components/form';
import { SettingRow, SettingsConfirmDialog } from '../../components';
import { webRequest } from '../../../../services/webClient';
import {
  isConfigured,
  type SettingsValues,
  type ReplyLanguage,
} from './videoDuplexSettingsValidation';
import './VideoDuplexModelSettings.css';

const DEFAULTS: SettingsValues = {
  video_live_provider: 'qwen_omni',
  joyai_api_base: '',
  joyai_api_key: '',
  joyai_model: '',
  qwen_omni_realtime_url: '',
  qwen_omni_api_key: '',
  qwen_omni_model: '',
  qwen_omni_voice: '',
  reply_language: 'match',
  voice_protocol: 'openai_http',
  voice_asr_endpoint: '',
  voice_tts_endpoint: '',
  voice_api_key: '',
  voice_asr_model: '',
  voice_tts_model: '',
  voice_tts_voice: '',
};

const SECRET_KEYS = ['qwen_omni_api_key'] as const;
type Payload = { values: SettingsValues; configured_secret_lengths: Record<string, number> };

function secretPlaceholder(length?: number): string {
  return Number.isSafeInteger(length) && Number(length) > 0 ? '*'.repeat(Number(length)) : '';
}

function ConfigField({
  field,
  value,
  label,
  secret,
  required = true,
  placeholder,
  onChange,
}: {
  field: keyof SettingsValues;
  value: string;
  label: string;
  secret?: boolean;
  required?: boolean;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="video-duplex-model-settings__field">
      <span>
        {label}{required ? ' *' : ''}
      </span>
      <input
        type={secret ? 'password' : 'text'}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        aria-required={required}
        data-testid="settings-video-duplex-config-field"
        data-variant={field}
        autoComplete="off"
      />
    </label>
  );
}

export function VideoDuplexModelSettings({
  disabled = false,
  enabled,
  onEnabledChange,
  onConfigured,
}: {
  disabled?: boolean;
  enabled: boolean;
  onEnabledChange: (enabled: boolean) => Promise<void>;
  onConfigured?: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [values, setValues] = useState<SettingsValues>(DEFAULTS);
  const [secretLengths, setSecretLengths] = useState<Record<string, number>>({});
  const [draft, setDraft] = useState<SettingsValues | null>(null);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [deleteError, setDeleteError] = useState('');
  const [deleting, setDeleting] = useState(false);
  const [error, setError] = useState('');
  const [connection, setConnection] = useState<'idle' | 'testing' | 'success' | 'error'>('idle');
  const [connectionError, setConnectionError] = useState('');

  const applyPayload = (payload: Payload) => {
    setValues({ ...DEFAULTS, ...payload.values });
    setSecretLengths(payload.configured_secret_lengths || {});
  };

  useEffect(() => {
    let active = true;
    void webRequest<Payload>('video.duplex.settings.get', {}, { timeoutMs: 10_000 })
      .then((payload) => {
        if (active) applyPayload(payload);
      })
      .catch((loadError: unknown) => {
        if (active) setError(loadError instanceof Error ? loadError.message : t('settingsPanel.videoDuplex.loadError'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, [t]);

  const configured = isConfigured(values, secretLengths);
  const modelName = values.qwen_omni_model;
  const providerName = 'Qwen Omni Realtime';
  const updateDraft = <K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) => {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };

  const openEditor = () => {
    setError('');
    setDraft({ ...values, video_live_provider: 'qwen_omni' });
  };

  const saveDraft = async () => {
    if (!draft || disabled || saving || deleting) return;
    if (!isConfigured(draft, secretLengths)) {
      setError(t('settingsPanel.videoDuplex.requiredFields'));
      return;
    }
    setSaving(true);
    setError('');
    const outgoing: Record<string, string> = {
      video_live_provider: 'qwen_omni',
      qwen_omni_realtime_url: draft.qwen_omni_realtime_url,
      qwen_omni_api_key: draft.qwen_omni_api_key,
      qwen_omni_model: draft.qwen_omni_model,
      qwen_omni_voice: draft.qwen_omni_voice,
      reply_language: draft.reply_language,
    };
    SECRET_KEYS.forEach((key) => {
      if (!draft[key].trim()) delete outgoing[key];
    });
    try {
      const payload = await webRequest<Payload>(
        'video.duplex.settings.update',
        { values: outgoing },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
      setConnection('idle');
      setConnectionError('');
      await onConfigured?.();
      setDraft(null);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.videoDuplex.saveError'));
    } finally {
      setSaving(false);
    }
  };

  const clearConfig = async () => {
    setDeleting(true);
    setDeleteError('');
    try {
      const emptyValues = Object.fromEntries(Object.keys(DEFAULTS).map((key) => [key, ''])) as Record<string, string>;
      emptyValues.video_live_provider = DEFAULTS.video_live_provider;
      emptyValues.reply_language = DEFAULTS.reply_language;
      emptyValues.voice_protocol = DEFAULTS.voice_protocol;
      const payload = await webRequest<Payload>(
        'video.duplex.settings.update',
        { values: emptyValues, clear_secrets: true },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
      setConnection('idle');
      setConnectionError('');
      if (enabled) await onEnabledChange(false);
      setDeleteOpen(false);
    } catch (clearError) {
      setDeleteError(clearError instanceof Error ? clearError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setDeleting(false);
    }
  };

  const testConnection = async () => {
    setConnection('testing');
    setConnectionError('');
    try {
      await webRequest('video.duplex.settings.test', {}, { timeoutMs: 20_000 });
      setConnection('success');
    } catch {
      setConnection('error');
      setConnectionError(t('settingsPanel.voice.connectionFailed'));
    }
  };

  const saveReplyLanguage = async (replyLanguage: ReplyLanguage) => {
    if (replyLanguage === values.reply_language || saving || deleting) return;
    setSaving(true);
    setError('');
    try {
      const payload = await webRequest<Payload>(
        'video.duplex.settings.update',
        { values: { reply_language: replyLanguage } },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.videoDuplex.saveError'));
    } finally {
      setSaving(false);
    }
  };

  const replyLanguageSelect = (
    value: ReplyLanguage,
    onChange: (value: ReplyLanguage) => void,
    options: { disabled?: boolean; testId?: string } = {},
  ) => (
    <SettingRow
      title={t('settingsPanel.videoDuplex.replyLanguageLabel')}
      description={t('settingsPanel.videoDuplex.replyLanguageDescription')}
    >
      <Select
        value={value}
        disabled={options.disabled}
        onChange={(next) => onChange(next as ReplyLanguage)}
        data-testid={options.testId || 'settings-video-duplex-reply-language'}
        aria-label={t('settingsPanel.videoDuplex.replyLanguageLabel')}
        options={[
          { value: 'match', label: t('settingsPanel.videoDuplex.replyLanguageMatch') },
          { value: 'zh-CN', label: t('settingsPanel.videoDuplex.replyLanguageZhCN') },
          { value: 'en', label: t('settingsPanel.videoDuplex.replyLanguageEn') },
        ]}
      />
    </SettingRow>
  );

  if (loading) {
    return (
      <SettingRow
        title={t('settingsPanel.fields.task_full_duplex_enabled.title')}
        description={t('settingsPanel.videoDuplex.loading')}
      />
    );
  }

  return (
    <>
      <SettingRow
        className="settings-agent-media__row"
        title={t('settingsPanel.fields.task_full_duplex_enabled.title')}
        description={t('settingsPanel.fields.task_full_duplex_enabled.description')}
        subSettings={
          configured ? (
            <>
              <div className="settings-agent-media__model-card">
                <img
                  src={qwenLogo}
                  alt=""
                  className="video-duplex-model-settings__model-icon"
                  aria-hidden="true"
                  data-testid="settings-video-duplex-model-icon"
                />
                <div className="video-duplex-model-settings__model-copy">
                  <strong className="settings-agent-media__model-name">{modelName}</strong>
                  <small>{providerName}</small>
                </div>
                {connection === 'success' ? (
                  <CheckCircle2
                    className="video-duplex-model-settings__connected"
                    aria-label={t('settingsPanel.voice.connectionSuccess')}
                    data-testid="settings-voice-connection-success"
                  />
                ) : null}
                <div className="settings-agent-media__actions">
                  <Button
                    variant="quiet"
                    size="sm"
                    icon={
                      connection === 'testing' ? (
                        <LoaderCircle className="chat-input-btn-icon--spin" aria-hidden />
                      ) : (
                        <settingsActionIcons.test aria-hidden />
                      )
                    }
                    title={t('settingsPanel.voice.testConnection')}
                    aria-label={t('settingsPanel.voice.testConnection')}
                    disabled={disabled || saving || deleting || connection === 'testing'}
                    onClick={() => void testConnection()}
                    data-testid="settings-voice-test-connection"
                  />
                  <Button
                    variant="quiet"
                    size="sm"
                    icon={<settingsActionIcons.edit aria-hidden />}
                    title={t('common.modify')}
                    aria-label={`${t('common.modify')} ${providerName}`}
                    disabled={disabled || saving || deleting || connection === 'testing'}
                    onClick={openEditor}
                    data-testid="settings-task-full-duplex-edit-btn"
                  />
                  <Button
                    variant="quiet"
                    size="sm"
                    icon={<settingsActionIcons.delete aria-hidden />}
                    title={t('common.delete')}
                    aria-label={`${t('common.delete')} ${providerName}`}
                    disabled={disabled || saving || deleting || connection === 'testing'}
                    onClick={() => {
                      setDeleteError('');
                      setDeleteOpen(true);
                    }}
                    data-testid="settings-task-full-duplex-delete-btn"
                  />
                </div>
              </div>
              {connectionError ? (
                <div
                  className="video-duplex-model-settings__error"
                  role="alert"
                  data-testid="settings-voice-connection-error"
                >
                  {connectionError}
                </div>
              ) : null}

              <div className="video-duplex-model-settings__reply-language">
                {replyLanguageSelect(values.reply_language, (value) => void saveReplyLanguage(value), {
                  disabled: saving || deleting,
                })}
              </div>
            </>
          ) : undefined
        }
      >
        {configured ? (
          <Switch
            checked={enabled}
            aria-label={t('settingsPanel.fields.task_full_duplex_enabled.title')}
            disabled={disabled || saving || deleting || connection === 'testing'}
            onChange={(next) => void onEnabledChange(next).catch(() => undefined)}
            data-testid="settings-task-full-duplex-switch"
          />
        ) : (
          <Button
            size="sm"
            variant="secondary"
            disabled={disabled || saving || deleting}
            onClick={openEditor}
            data-testid="settings-voice-configure"
          >
            {t('settingsPanel.common.configure')}
          </Button>
        )}
      </SettingRow>
      {error && !draft ? (
        <div className="video-duplex-model-settings__error" role="alert" data-testid="settings-voice-error">
          {error}
        </div>
      ) : null}

      {draft ? (
        <FormDialog
          open
          title={t('settingsPanel.videoDuplex.dialogTitle')}
          submitting={saving}
          confirmDisabled={disabled || deleting || !isConfigured(draft, secretLengths)}
          description={t('settingsPanel.videoDuplex.requiredFields')}
          confirmLabel={t('common.save')}
          cancelLabel={t('common.cancel')}
          dialogClassName="settings-model-dialog"
          testIdPrefix="settings-task-full-duplex-config-dialog"
          onConfirm={() => void saveDraft()}
          onCancel={() => {
            if (!saving) {
              setDraft(null);
              setError('');
            }
          }}
        >
          <div className="video-duplex-model-settings__dialog-fields">
            {replyLanguageSelect(draft.reply_language, (value) => updateDraft('reply_language', value), {
              testId: 'settings-video-duplex-reply-language-draft',
            })}
            <label className="video-duplex-model-settings__field">
              <span>{t('settingsPanel.videoDuplex.providerLabel')}</span>
              <input
                value="Qwen Omni Realtime"
                readOnly
                data-testid="settings-video-duplex-provider"
              />
            </label>
            <ConfigField
              field="qwen_omni_realtime_url"
              value={draft.qwen_omni_realtime_url}
              label="Qwen Realtime WebSocket"
              onChange={(value) => updateDraft('qwen_omni_realtime_url', value)}
            />
            <ConfigField
              field="qwen_omni_api_key"
              value={draft.qwen_omni_api_key}
              label="Qwen API Key"
              secret
              placeholder={secretPlaceholder(secretLengths.qwen_omni_api_key)}
              onChange={(value) => updateDraft('qwen_omni_api_key', value)}
            />
            <ConfigField
              field="qwen_omni_model"
              value={draft.qwen_omni_model}
              label={t('settingsPanel.videoDuplex.qwenModelLabel')}
              onChange={(value) => updateDraft('qwen_omni_model', value)}
            />
            <ConfigField
              field="qwen_omni_voice"
              value={draft.qwen_omni_voice}
              required={false}
              label={t('settingsPanel.videoDuplex.qwenVoiceLabel')}
              onChange={(value) => updateDraft('qwen_omni_voice', value)}
            />
          </div>
          {error ? (
            <div className="settings-page__error" role="alert">
              {error}
            </div>
          ) : null}
        </FormDialog>
      ) : null}
      <SettingsConfirmDialog
        open={deleteOpen}
        title={t('settingsPanel.videoDuplex.deleteTitle')}
        message={t('settingsPanel.videoDuplex.deleteConfirm')}
        confirming={deleting}
        error={deleteError}
        onConfirm={() => void clearConfig()}
        onCancel={() => {
          if (!deleting) setDeleteOpen(false);
        }}
      />
    </>
  );
}
