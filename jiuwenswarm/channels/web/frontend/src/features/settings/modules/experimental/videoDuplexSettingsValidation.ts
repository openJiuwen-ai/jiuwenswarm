export type Provider = 'joyai' | 'qwen_omni' | 'doubao';
export type VoiceProtocol = 'native_ws' | 'openai_http';
export type ReplyLanguage = 'match' | 'zh-CN' | 'en';

export type SettingsValues = {
  video_live_provider: Provider;
  joyai_api_base: string;
  joyai_api_key: string;
  joyai_model: string;
  qwen_omni_realtime_url: string;
  qwen_omni_api_key: string;
  qwen_omni_model: string;
  qwen_omni_voice: string;
  doubao_realtime_url: string;
  doubao_model: string;
  doubao_voice: string;
  doubao_api_key: string;
  reply_language: ReplyLanguage;
  voice_protocol: VoiceProtocol;
  voice_asr_endpoint: string;
  voice_tts_endpoint: string;
  voice_api_key: string;
  voice_asr_model: string;
  voice_tts_model: string;
  voice_tts_voice: string;
};

export const EMPTY_SETTINGS: SettingsValues = {
  video_live_provider: 'qwen_omni',
  joyai_api_base: '',
  joyai_api_key: '',
  joyai_model: '',
  qwen_omni_realtime_url: '',
  qwen_omni_api_key: '',
  qwen_omni_model: '',
  qwen_omni_voice: '',
  doubao_realtime_url: '',
  doubao_model: '',
  doubao_voice: '',
  doubao_api_key: '',
  reply_language: 'match',
  voice_protocol: 'openai_http',
  voice_asr_endpoint: '',
  voice_tts_endpoint: '',
  voice_api_key: '',
  voice_asr_model: '',
  voice_tts_model: '',
  voice_tts_voice: '',
};

export const REALTIME_SECRET_KEYS = ['qwen_omni_api_key', 'doubao_api_key'] as const;
export const SECRET_KEYS = ['joyai_api_key', ...REALTIME_SECRET_KEYS, 'voice_api_key'] as const;

export interface SettingsPayload {
  enabled: boolean;
  values: SettingsValues;
  defaults: Partial<SettingsValues>;
  configured_secret_lengths: Record<string, number>;
  restart_required?: boolean;
}

export function secretPlaceholder(length?: number): string {
  return Number.isSafeInteger(length) && Number(length) > 0 ? '*'.repeat(Number(length)) : '';
}

export function isConfigured(values: SettingsValues, secretLengths: Record<string, number>): boolean {
  const hasSecret = (key: 'qwen_omni_api_key' | 'doubao_api_key') =>
    Boolean(values[key].trim() || secretLengths[key] > 0);
  if (values.video_live_provider === 'doubao') return hasSecret('doubao_api_key');
  if (values.video_live_provider === 'qwen_omni') {
    return hasSecret('qwen_omni_api_key');
  }
  return false;
}
