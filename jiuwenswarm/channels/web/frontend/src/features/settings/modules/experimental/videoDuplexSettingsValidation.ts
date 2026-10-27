export type Provider = 'joyai' | 'qwen_omni';
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
  reply_language: ReplyLanguage;
  voice_protocol: VoiceProtocol;
  voice_asr_endpoint: string;
  voice_tts_endpoint: string;
  voice_api_key: string;
  voice_asr_model: string;
  voice_tts_model: string;
  voice_tts_voice: string;
};

export function isConfigured(values: SettingsValues, secretLengths: Record<string, number>): boolean {
  const hasSecret = (key: 'joyai_api_key' | 'qwen_omni_api_key' | 'voice_api_key') =>
    Boolean(values[key].trim() || secretLengths[key] > 0);
  if (values.video_live_provider === 'qwen_omni') {
    return Boolean(
      values.qwen_omni_realtime_url.trim() && values.qwen_omni_model.trim() && hasSecret('qwen_omni_api_key'),
    );
  }
  const modelConfigured = Boolean(
    values.joyai_api_base.trim() && values.joyai_model.trim() && hasSecret('joyai_api_key'),
  );
  const endpointsConfigured = Boolean(values.voice_asr_endpoint.trim() && values.voice_tts_endpoint.trim());
  if (values.voice_protocol === 'native_ws') {
    return modelConfigured && endpointsConfigured && Boolean(values.voice_tts_voice.trim());
  }
  return modelConfigured && endpointsConfigured && hasSecret('voice_api_key') && Boolean(
    values.voice_asr_model.trim() && values.voice_tts_model.trim(),
  );
}
