import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { settingsActionIcons } from '../../../../assets/settings';
import { Button } from '../../../../components/ui';
import { FormDialog } from '../../../../components/form';
import { SettingsConfirmDialog } from '../../components';
import { webRequest } from '../../../../services/webClient';
import './VideoDuplexModelSettings.css';

type Provider = 'joyai' | 'qwen_omni';
type VoiceProtocol = 'native_ws' | 'openai_http';

type SettingsValues = {
  video_live_provider: Provider;
  joyai_api_base: string;
  joyai_api_key: string;
  joyai_model: string;
  qwen_omni_realtime_url: string;
  qwen_omni_api_key: string;
  qwen_omni_model: string;
  qwen_omni_voice: string;
  voice_protocol: VoiceProtocol;
  voice_asr_endpoint: string;
  voice_tts_endpoint: string;
  voice_api_key: string;
  voice_asr_model: string;
  voice_tts_model: string;
  voice_tts_voice: string;
};

const DEFAULTS: SettingsValues = {
  video_live_provider: 'joyai',
  joyai_api_base: '',
  joyai_api_key: '',
  joyai_model: 'jdopensource/JoyAI-VL-Interaction',
  qwen_omni_realtime_url: '',
  qwen_omni_api_key: '',
  qwen_omni_model: 'qwen3.5-omni-flash-realtime',
  qwen_omni_voice: 'Cherry',
  voice_protocol: 'native_ws',
  voice_asr_endpoint: 'ws://127.0.0.1:8994/ws/asr',
  voice_tts_endpoint: 'ws://127.0.0.1:8992/ws/tts',
  voice_api_key: '',
  voice_asr_model: '',
  voice_tts_model: '',
  voice_tts_voice: 'vivian',
};

const SECRET_KEYS = ['joyai_api_key', 'qwen_omni_api_key', 'voice_api_key'] as const;
type Payload = { values: SettingsValues; configured_secret_lengths: Record<string, number> };

function secretPlaceholder(length?: number): string {
  return Number.isSafeInteger(length) && Number(length) > 0 ? '*'.repeat(Number(length)) : '';
}

function isConfigured(values: SettingsValues, secretLengths: Record<string, number>): boolean {
  if (values.video_live_provider === 'qwen_omni') {
    return Boolean(values.qwen_omni_realtime_url.trim() && values.qwen_omni_model.trim());
  }
  return Boolean(
    values.joyai_api_base.trim() &&
      values.joyai_model.trim() &&
      (values.joyai_api_key.trim() || secretLengths.joyai_api_key),
  );
}

function ConfigField({
  value,
  label,
  secret,
  placeholder,
  onChange,
}: {
  value: string;
  label: string;
  secret?: boolean;
  placeholder?: string;
  onChange: (value: string) => void;
}) {
  return (
    <label className="video-duplex-model-settings__field">
      <span>{label}</span>
      <input
        type={secret ? 'password' : 'text'}
        value={value}
        placeholder={placeholder}
        onChange={(event) => onChange(event.target.value)}
        autoComplete="off"
      />
    </label>
  );
}

export function VideoDuplexModelSettings() {
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
  }, []);

  const configured = isConfigured(values, secretLengths);
  const modelName = values.video_live_provider === 'qwen_omni' ? values.qwen_omni_model : values.joyai_model;
  const providerName = values.video_live_provider === 'qwen_omni' ? 'Qwen Omni Realtime' : 'JoyAI';
  const updateDraft = <K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) => {
    setDraft((current) => (current ? { ...current, [key]: value } : current));
  };

  const openEditor = () => {
    setError('');
    setDraft({ ...values });
  };

  const saveDraft = async () => {
    if (!draft) return;
    setSaving(true);
    setError('');
    const outgoing: Record<string, string> = { ...draft };
    SECRET_KEYS.forEach((key) => {
      if (!draft[key]) delete outgoing[key];
    });
    try {
      const payload = await webRequest<Payload>(
        'video.duplex.settings.update',
        { values: outgoing },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
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
      emptyValues.video_live_provider = 'joyai';
      const payload = await webRequest<Payload>(
        'video.duplex.settings.update',
        { values: emptyValues, clear_secrets: true },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
      setDeleteOpen(false);
    } catch (clearError) {
      setDeleteError(clearError instanceof Error ? clearError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setDeleting(false);
    }
  };

  if (loading) {
    return <div className="video-duplex-model-settings__status">{t('settingsPanel.videoDuplex.loading')}</div>;
  }

  return (
    <>
      {error ? <div className="video-duplex-model-settings__error" role="alert">{error}</div> : null}
      {configured ? (
        <div className="settings-agent-media__model-card">
          <div className="video-duplex-model-settings__model-copy">
            <strong className="settings-agent-media__model-name">{modelName}</strong>
            <small>{providerName}</small>
          </div>
          <div className="settings-agent-media__actions">
            <Button
              variant="quiet"
              size="sm"
              icon={<settingsActionIcons.edit aria-hidden />}
              title={t('common.modify')}
              aria-label={`${t('common.modify')} ${providerName}`}
              disabled={saving || deleting}
              onClick={openEditor}
              data-testid="settings-task-full-duplex-edit-btn"
            />
            <Button
              variant="quiet"
              size="sm"
              icon={<settingsActionIcons.delete aria-hidden />}
              title={t('common.delete')}
              aria-label={`${t('common.delete')} ${providerName}`}
              disabled={saving || deleting}
              onClick={() => {
                setDeleteError('');
                setDeleteOpen(true);
              }}
              data-testid="settings-task-full-duplex-delete-btn"
            />
          </div>
        </div>
      ) : (
        <div className="settings-agent-media__model-card">
          <span className="settings-agent-media__model-name">{t('settingsPanel.videoDuplex.notConfigured')}</span>
          <Button size="sm" variant="primary" disabled={saving || deleting} onClick={openEditor}>
            {t('settingsPanel.common.configure')}
          </Button>
        </div>
      )}

      {draft ? (
        <FormDialog
          open
          title={t('settingsPanel.videoDuplex.dialogTitle')}
          submitting={saving}
          confirmLabel={t('common.save')}
          cancelLabel={t('common.cancel')}
          dialogClassName="settings-model-dialog"
          testIdPrefix="settings-task-full-duplex-config-dialog"
          onConfirm={() => void saveDraft()}
          onCancel={() => {
            if (!saving) setDraft(null);
          }}
        >
          <div className="video-duplex-model-settings__dialog-fields">
            <label className="video-duplex-model-settings__field">
              <span>{t('settingsPanel.videoDuplex.providerLabel')}</span>
              <select
                value={draft.video_live_provider}
                onChange={(event) => updateDraft('video_live_provider', event.target.value as Provider)}
              >
                <option value="joyai">JoyAI</option>
                <option value="qwen_omni">Qwen Omni Realtime</option>
              </select>
            </label>
            {draft.video_live_provider === 'joyai' ? (
              <>
                <ConfigField value={draft.joyai_api_base} label="JoyAI API Base" placeholder="http://127.0.0.1:8070/v1" onChange={(value) => updateDraft('joyai_api_base', value)} />
                <ConfigField value={draft.joyai_api_key} label="JoyAI API Key" secret placeholder={secretPlaceholder(secretLengths.joyai_api_key)} onChange={(value) => updateDraft('joyai_api_key', value)} />
                <ConfigField value={draft.joyai_model} label={t('settingsPanel.videoDuplex.joyaiModelLabel')} onChange={(value) => updateDraft('joyai_model', value)} />
                <h3>{t('settingsPanel.videoDuplex.voiceSectionTitle')}</h3>
                <label className="video-duplex-model-settings__field">
                  <span>{t('settingsPanel.videoDuplex.voiceProtocolLabel')}</span>
                  <select value={draft.voice_protocol} onChange={(event) => updateDraft('voice_protocol', event.target.value as VoiceProtocol)}>
                    <option value="native_ws">JoyAI WebSocket</option>
                    <option value="openai_http">OpenAI HTTP</option>
                  </select>
                </label>
                <ConfigField value={draft.voice_asr_endpoint} label={t('settingsPanel.videoDuplex.asrEndpointLabel')} onChange={(value) => updateDraft('voice_asr_endpoint', value)} />
                <ConfigField value={draft.voice_tts_endpoint} label={t('settingsPanel.videoDuplex.ttsEndpointLabel')} onChange={(value) => updateDraft('voice_tts_endpoint', value)} />
                {draft.voice_protocol === 'openai_http' ? (
                  <>
                    <ConfigField value={draft.voice_api_key} label={t('settingsPanel.videoDuplex.voiceApiKeyLabel')} secret placeholder={secretPlaceholder(secretLengths.voice_api_key)} onChange={(value) => updateDraft('voice_api_key', value)} />
                    <ConfigField value={draft.voice_asr_model} label={t('settingsPanel.videoDuplex.asrModelLabel')} onChange={(value) => updateDraft('voice_asr_model', value)} />
                    <ConfigField value={draft.voice_tts_model} label={t('settingsPanel.videoDuplex.ttsModelLabel')} onChange={(value) => updateDraft('voice_tts_model', value)} />
                    <ConfigField value={draft.voice_tts_voice} label={t('settingsPanel.videoDuplex.ttsVoiceLabel')} onChange={(value) => updateDraft('voice_tts_voice', value)} />
                  </>
                ) : null}
              </>
            ) : (
              <>
                <ConfigField value={draft.qwen_omni_realtime_url} label="Qwen Realtime WebSocket" onChange={(value) => updateDraft('qwen_omni_realtime_url', value)} />
                <ConfigField value={draft.qwen_omni_api_key} label="Qwen API Key" secret placeholder={secretPlaceholder(secretLengths.qwen_omni_api_key)} onChange={(value) => updateDraft('qwen_omni_api_key', value)} />
                <ConfigField value={draft.qwen_omni_model} label={t('settingsPanel.videoDuplex.qwenModelLabel')} onChange={(value) => updateDraft('qwen_omni_model', value)} />
                <ConfigField value={draft.qwen_omni_voice} label={t('settingsPanel.videoDuplex.qwenVoiceLabel')} onChange={(value) => updateDraft('qwen_omni_voice', value)} />
              </>
            )}
          </div>
          {error ? <div className="settings-page__error" role="alert">{error}</div> : null}
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
