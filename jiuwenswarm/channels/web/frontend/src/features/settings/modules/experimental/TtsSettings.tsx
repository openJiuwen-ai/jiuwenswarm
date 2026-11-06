import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { playAudioBase64 } from '../../../../utils/tts';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps, SettingSelectConfigurationProps, SettingSelectOption } from '../../registry/types';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { useSettingsSource } from '../../services/SettingsSourceProvider';
import { AgentConfigDialog } from '../agent/AgentSettings';

type VoiceCatalog = {
  default_url: string;
  default_model: string;
  default_voice: string;
  models: string[];
  voices: Array<{ id: string; name: string; models: string[] }>;
};

function catalogs(values: Readonly<Record<string, unknown>>): Record<string, VoiceCatalog> {
  return (values.tts_voice_catalogs ?? {}) as Record<string, VoiceCatalog>;
}

export function ttsProviderOptions(values: Readonly<Record<string, unknown>>): SettingSelectOption[] {
  return [
    { value: 'cosyvoice', labelKey: 'settingsPanel.tts.cosyvoice' },
    { value: 'qwen_omni', labelKey: 'settingsPanel.tts.qwen' },
  ].filter((option) => Boolean(catalogs(values)[option.value]));
}

export function ttsModelOptions(values: Readonly<Record<string, unknown>>): SettingSelectOption[] {
  return (catalogs(values)[String(values.tts_provider)]?.models ?? []).map((value) => ({ value, label: value }));
}

export function ttsVoiceOptions(values: Readonly<Record<string, unknown>>): SettingSelectOption[] {
  return (catalogs(values)[String(values.tts_provider)]?.voices ?? [])
    .filter((voice) => voice.models.includes(String(values.tts_model)))
    .map((voice) => ({ value: voice.id, label: voice.name }));
}

export function ttsModelUpdates(value: string | number | boolean, values: Readonly<Record<string, unknown>>) {
  const compatible = (catalogs(values)[String(values.tts_provider)]?.voices ?? [])
    .filter((voice) => voice.models.includes(String(value)));
  const voice = compatible.find((entry) => entry.id === values.tts_voice) ?? compatible[0];
  if (!voice) throw new Error('No compatible voice for the selected TTS model');
  return { tts_model: String(value), tts_voice: voice.id };
}

export function TtsProviderConfiguration({ value, onSave, onClose }: SettingSelectConfigurationProps) {
  const source = useSettingsSource();
  const provider = String(value);
  const catalog = catalogs(source.values)[provider];
  return (
    <AgentConfigDialog
      titleKey="settingsPanel.tts.switchTitle"
      fields={['tts_api_base', 'tts_api_key']}
      config={{ tts_api_base: catalog.default_url }}
      save={(updates) => onSave({
        ...updates,
        tts_provider: provider,
        tts_model: catalog.default_model,
        tts_voice: catalog.default_voice,
      })}
      onClose={onClose}
    />
  );
}

export function TtsSynthesisCheck({ disabled, onEditingChange }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const { isConnected, request } = useSettingsServices();
  const [testing, setTesting] = useState(false);
  const [feedback, setFeedback] = useState<{ success: boolean; message: string } | null>(null);
  useEffect(() => {
    onEditingChange?.(testing);
    return () => onEditingChange?.(false);
  }, [onEditingChange, testing]);
  const test = async () => {
    if (disabled || !isConnected || testing) return;
    setTesting(true);
    setFeedback(null);
    try {
      const response = await request<{
        success: boolean;
        audio_base64?: string;
        audio_mime?: string;
        error?: string;
      }>('tts.test', {}, { timeoutMs: 70_000 });
      if (!response.success || !response.audio_base64) {
        throw new Error(response.error || t('settingsPanel.tts.testFailed'));
      }
      const played = await playAudioBase64(response.audio_base64, response.audio_mime);
      setFeedback({ success: played, message: t(played ? 'settingsPanel.tts.testSuccess' : 'settingsPanel.tts.playbackFailed') });
    } catch (error) {
      setFeedback({ success: false, message: error instanceof Error ? error.message : t('settingsPanel.tts.testFailed') });
    } finally {
      setTesting(false);
    }
  };
  return (
    <SettingRow
      title={<span data-testid="settings-tts-test-title">{t('settingsPanel.tts.testTitle')}</span>}
      description={<span data-testid="settings-tts-test-description">{t('settingsPanel.tts.testDescription')}</span>}
      subSettings={feedback ? (
        <div
          className={feedback.success ? 'p-4 text-sm text-text-secondary' : 'settings-page__error'}
          role={feedback.success ? 'status' : 'alert'}
          data-testid="settings-tts-test-feedback"
          data-variant={feedback.success ? 'success' : 'error'}
        >
          {feedback.message}
        </div>
      ) : undefined}
    >
      <Button
        disabled={disabled || !isConnected || testing}
        loading={testing}
        onClick={() => void test()}
        data-testid="settings-tts-test-btn"
        data-variant={testing ? 'testing' : 'idle'}
      >
        {t(testing ? 'settingsPanel.tts.testing' : 'settingsPanel.tts.testButton')}
      </Button>
    </SettingRow>
  );
}
