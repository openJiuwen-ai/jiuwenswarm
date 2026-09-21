import { FormEvent, useEffect, useState } from 'react';
import { LoaderCircle, Save } from 'lucide-react';
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
  video_live_provider: 'joyai', joyai_api_base: '', joyai_api_key: '',
  joyai_model: 'jdopensource/JoyAI-VL-Interaction', qwen_omni_realtime_url: '',
  qwen_omni_api_key: '', qwen_omni_model: 'qwen3.5-omni-flash-realtime', qwen_omni_voice: 'Cherry',
  voice_protocol: 'native_ws', voice_asr_endpoint: 'ws://127.0.0.1:8994/ws/asr',
  voice_tts_endpoint: 'ws://127.0.0.1:8992/ws/tts', voice_api_key: '', voice_asr_model: '',
  voice_tts_model: '', voice_tts_voice: 'vivian',
};
const SECRET_KEYS = ['joyai_api_key', 'qwen_omni_api_key', 'voice_api_key'] as const;
type Payload = { values: SettingsValues; configured_secret_lengths: Record<string, number> };

export function VideoDuplexModelSettings() {
  const [values, setValues] = useState<SettingsValues>(DEFAULTS);
  const [secretLengths, setSecretLengths] = useState<Record<string, number>>({});
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [message, setMessage] = useState('');
  const [error, setError] = useState('');

  const applyPayload = (payload: Payload) => {
    setValues({ ...DEFAULTS, ...payload.values });
    setSecretLengths(payload.configured_secret_lengths || {});
  };
  useEffect(() => {
    let active = true;
    void webRequest<Payload & { enabled?: boolean }>('video.duplex.settings.get', {}, { timeoutMs: 10_000 })
      .then((payload) => { if (active) applyPayload(payload); })
      .catch((loadError: unknown) => { if (active) setError(loadError instanceof Error ? loadError.message : '无法读取全双工模型配置'); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, []);

  const update = <K extends keyof SettingsValues>(key: K, value: SettingsValues[K]) => {
    setValues((current) => ({ ...current, [key]: value }));
    setMessage('');
  };
  const field = (key: keyof SettingsValues, label: string, secret = false) => (
    <label className="video-duplex-model-settings__field" key={key}>
      <span>{label}</span>
      <input type={secret ? 'password' : 'text'} value={values[key]} placeholder={secret ? '*'.repeat(secretLengths[key] || 0) : undefined}
        onChange={(event) => update(key, event.target.value as SettingsValues[typeof key])} autoComplete="off" />
    </label>
  );
  const save = async (event: FormEvent) => {
    event.preventDefault(); setSaving(true); setError(''); setMessage('');
    const outgoing: Record<string, string> = { ...values };
    SECRET_KEYS.forEach((key) => { if (!values[key]) delete outgoing[key]; });
    try {
      const payload = await webRequest<Payload>('video.duplex.settings.update', { values: outgoing }, { timeoutMs: 10_000 });
      applyPayload(payload); setMessage('全双工模型配置已保存');
    } catch (saveError) { setError(saveError instanceof Error ? saveError.message : '无法保存全双工模型配置'); }
    finally { setSaving(false); }
  };

  if (loading) return <div className="video-duplex-model-settings__status"><LoaderCircle className="is-spinning" />正在读取全双工模型配置…</div>;
  return <form className="video-duplex-model-settings" onSubmit={(event) => void save(event)}>
    <p className="video-duplex-model-settings__description">配置全双工视觉模型，以及 JoyAI 所需的语音转写与语音播报通道。</p>
    {error && <div className="video-duplex-model-settings__error" role="alert">{error}</div>}
    {message && <div className="video-duplex-model-settings__success" role="status">{message}</div>}
    <h3>视觉模型</h3>
    <label className="video-duplex-model-settings__field"><span>模型通道</span><select value={values.video_live_provider} onChange={(event) => update('video_live_provider', event.target.value as Provider)}><option value="joyai">JoyAI</option><option value="qwen_omni">Qwen Omni Realtime</option></select></label>
    {values.video_live_provider === 'joyai' && <>
      {field('joyai_api_base', 'JoyAI API Base')}{field('joyai_api_key', 'JoyAI API Key', true)}{field('joyai_model', 'JoyAI 模型')}
      <h3>语音转写与语音播报</h3>
      <label className="video-duplex-model-settings__field"><span>语音通道</span><select value={values.voice_protocol} onChange={(event) => update('voice_protocol', event.target.value as VoiceProtocol)}><option value="native_ws">JoyAI WebSocket</option><option value="openai_http">OpenAI HTTP</option></select></label>
      {field('voice_asr_endpoint', '语音转写完整接口')}{field('voice_tts_endpoint', '语音播报完整接口')}
      {values.voice_protocol === 'openai_http' && <>{field('voice_api_key', '语音 API Key', true)}{field('voice_asr_model', '语音转写模型')}{field('voice_tts_model', '语音播报模型')}{field('voice_tts_voice', '语音播报音色')}</>}
    </>}
    {values.video_live_provider === 'qwen_omni' && <>{field('qwen_omni_realtime_url', 'Qwen Realtime WebSocket')}{field('qwen_omni_api_key', 'Qwen API Key', true)}{field('qwen_omni_model', 'Qwen 模型')}{field('qwen_omni_voice', 'Qwen 音色')}</>}
    <footer><button type="submit" disabled={saving}>{saving ? <LoaderCircle className="is-spinning" /> : <Save />}{saving ? '保存中' : '保存设置'}</button></footer>
  </form>;
}
