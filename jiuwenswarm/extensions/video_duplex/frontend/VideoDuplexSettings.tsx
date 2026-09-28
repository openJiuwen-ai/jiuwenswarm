import { FormEvent, useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { LoaderCircle, Power, Save, Settings2 } from 'lucide-react';

import type { ApplicationPluginSettingsProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
import './VideoDuplexSettings.css';

type Provider = 'joyai' | 'qwen_omni';
type VoiceProtocol = 'native_ws' | 'openai_http';

interface SettingsValues {
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
}

interface SettingsPayload {
  enabled: boolean;
  values: SettingsValues;
  configured_secret_lengths: Record<string, number>;
  restart_required?: boolean;
}

const EMPTY_SETTINGS: SettingsValues = {
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

function secretPlaceholder(length?: number): string {
  return Number.isSafeInteger(length) && Number(length) > 0 ? '*'.repeat(Number(length)) : '';
}

export interface VideoDuplexSettingsProps extends Partial<ApplicationPluginSettingsProps> {
  /** Render the configuration directly inside Settings > Experimental. */
  embedded?: boolean;
  /** The plugin tab no longer owns the enable switch when embedded. */
  showEnableControl?: boolean;
}

export function VideoDuplexSettings({
  contribution,
  onManifestChanged,
  embedded = false,
  showEnableControl = !embedded,
}: VideoDuplexSettingsProps) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(contribution?.enabled !== false);
  const [expanded, setExpanded] = useState(embedded);
  const [values, setValues] = useState<SettingsValues>(EMPTY_SETTINGS);
  const [secretLengths, setSecretLengths] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');
  const [notice, setNotice] = useState('');

  const applyPayload = (payload: SettingsPayload) => {
    setEnabled(payload.enabled);
    setValues({ ...EMPTY_SETTINGS, ...payload.values });
    setSecretLengths(payload.configured_secret_lengths || {});
  };

  useEffect(() => {
    let active = true;
    setLoading(true);
    void webRequest<SettingsPayload>('video.duplex.settings.get', {}, { timeoutMs: 10_000 })
      .then(payload => {
        if (active) applyPayload(payload);
      })
      .catch((loadError: unknown) => {
        if (active) setError(loadError instanceof Error ? loadError.message : t('settingsPanel.videoDuplex.loadError'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, []);

  const updateValue = <K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) => {
    setValues(current => ({ ...current, [key]: value }));
    setNotice('');
  };

  const toggleEnabled = async () => {
    if (saving || loading) return;
    setSaving(true);
    setError('');
    setNotice('');
    try {
      const payload = await webRequest<SettingsPayload>(
        'video.duplex.settings.set_enabled',
        { enabled: !enabled },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
      setNotice(payload.enabled ? t('settingsPanel.videoDuplex.enabled') : t('settingsPanel.videoDuplex.disabled'));
      onManifestChanged?.();
    } catch (toggleError) {
      setError(toggleError instanceof Error ? toggleError.message : t('settingsPanel.videoDuplex.toggleError'));
    } finally {
      setSaving(false);
    }
  };

  const save = async (event: FormEvent) => {
    event.preventDefault();
    setSaving(true);
    setError('');
    setNotice('');
    const outgoing: Record<string, string> = { ...values };
    for (const key of SECRET_KEYS) {
      if (!values[key]) delete outgoing[key];
    }
    try {
      const payload = await webRequest<SettingsPayload>(
        'video.duplex.settings.update',
        { values: outgoing },
        { timeoutMs: 10_000 },
      );
      applyPayload(payload);
      setNotice(t('settingsPanel.videoDuplex.saved'));
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.videoDuplex.saveError'));
    } finally {
      setSaving(false);
    }
  };

  const field = (
    key: keyof SettingsValues,
    label: string,
    options: { secret?: boolean; placeholder?: string } = {},
  ) => (
    <label className="video-duplex-settings__field">
      <span>{label}</span>
      <input
        type={options.secret ? 'password' : 'text'}
        value={values[key]}
        placeholder={options.secret ? secretPlaceholder(secretLengths[key]) : options.placeholder}
        onChange={event => updateValue(key, event.target.value as SettingsValues[typeof key])}
        autoComplete="off"
      />
    </label>
  );

  return (
    <div className="video-duplex-settings">
      <div className="video-duplex-settings__summary">
        <div>
          <p>{t('settingsPanel.videoDuplex.description')}</p>
          {error && <small className="is-error">{error}</small>}
          {notice && <small className="is-success">{notice}</small>}
        </div>
        <div className="video-duplex-settings__actions">
          {showEnableControl && (
            <button type="button" className={enabled ? 'is-danger' : ''} disabled={loading || saving} onClick={() => void toggleEnabled()}>
              {loading || saving ? <LoaderCircle className="is-spinning" aria-hidden /> : <Power aria-hidden />}
              {enabled ? t('settingsPanel.videoDuplex.disable') : t('settingsPanel.videoDuplex.enable')}
            </button>
          )}
          {!embedded && (
            <button type="button" disabled={loading || saving} onClick={() => setExpanded(current => !current)}>
              <Settings2 aria-hidden />
              {expanded ? t('common.collapse') : t('settingsPanel.common.configure')}
            </button>
          )}
        </div>
      </div>

      {expanded && (
        <form className="video-duplex-settings__form" onSubmit={event => void save(event)}>
          <h3>{t('settingsPanel.videoDuplex.videoSectionTitle')}</h3>
          <label className="video-duplex-settings__field">
            <span>{t('settingsPanel.videoDuplex.providerLabel')}</span>
            <select value={values.video_live_provider} onChange={event => updateValue('video_live_provider', event.target.value as Provider)}>
              <option value="joyai">JoyAI</option>
              <option value="qwen_omni">Qwen Omni Realtime</option>
            </select>
          </label>

          {values.video_live_provider === 'joyai' ? (
            <>
              {field('joyai_api_base', 'JoyAI API Base', { placeholder: 'http://127.0.0.1:8070/v1' })}
              {field('joyai_api_key', 'JoyAI API Key', { secret: true })}
              {field('joyai_model', t('settingsPanel.videoDuplex.joyaiModelLabel'))}

              <h3>{t('settingsPanel.videoDuplex.voiceSectionTitle')}</h3>
              <label className="video-duplex-settings__field">
                <span>{t('settingsPanel.videoDuplex.voiceProtocolLabel')}</span>
                <select value={values.voice_protocol} onChange={event => updateValue('voice_protocol', event.target.value as VoiceProtocol)}>
                  <option value="native_ws">JoyAI WebSocket</option>
                  <option value="openai_http">OpenAI HTTP</option>
                </select>
              </label>
              {field('voice_asr_endpoint', t('settingsPanel.videoDuplex.asrEndpointLabel'))}
              {field('voice_tts_endpoint', t('settingsPanel.videoDuplex.ttsEndpointLabel'))}
              {values.voice_protocol === 'openai_http' && (
                <>
                  {field('voice_api_key', t('settingsPanel.videoDuplex.voiceApiKeyLabel'), { secret: true })}
                  {field('voice_asr_model', t('settingsPanel.videoDuplex.asrModelLabel'))}
                  {field('voice_tts_model', t('settingsPanel.videoDuplex.ttsModelLabel'))}
                  {field('voice_tts_voice', t('settingsPanel.videoDuplex.ttsVoiceLabel'))}
                </>
              )}
            </>
          ) : (
            <>
              {field('qwen_omni_realtime_url', 'Qwen Realtime WebSocket')}
              {field('qwen_omni_api_key', 'Qwen API Key', { secret: true })}
              {field('qwen_omni_model', t('settingsPanel.videoDuplex.qwenModelLabel'))}
              {field('qwen_omni_voice', t('settingsPanel.videoDuplex.qwenVoiceLabel'))}
            </>
          )}

          <footer>
            <button type="submit" className="is-primary" disabled={saving}>
              {saving ? <LoaderCircle className="is-spinning" aria-hidden /> : <Save aria-hidden />}
              {saving ? t('common.saving') : t('settingsPanel.videoDuplex.saveSettings')}
            </button>
          </footer>
        </form>
      )}
    </div>
  );
}
