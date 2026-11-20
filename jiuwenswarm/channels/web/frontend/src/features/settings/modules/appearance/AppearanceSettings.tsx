import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button, Input, Select, type SelectOption } from '../../../../components/ui';
import { SettingRow } from '../../components';
import type { SettingsCustomItemProps } from '../../registry/types';
import {
  applyAppearancePreferences,
  clearAppearancePreferences,
  defaultAppearancePreferences,
  FONT_SCALE_STEPS,
  readAppearancePreferences,
  saveAppearancePreferences,
  type AppearancePreferences,
  type AppearanceStorageLike,
  type AppearanceStyleTarget,
} from './appearancePreferences';
import './AppearanceSettings.css';

const FONT_SCALE_LABEL_KEYS: Record<number, string> = {
  90: 'settingsPanel.appearance.fontSizeSmaller',
  100: 'settingsPanel.appearance.fontSizeDefault',
  110: 'settingsPanel.appearance.fontSizeLarger',
  125: 'settingsPanel.appearance.fontSizeLarge',
  140: 'settingsPanel.appearance.fontSizeXLarge',
};

function browserLocalStorage(): AppearanceStorageLike | null {
  try {
    return typeof window === 'undefined' ? null : window.localStorage;
  } catch {
    return null;
  }
}

function rootStyleTarget(): AppearanceStyleTarget | null {
  return typeof document === 'undefined' ? null : document.documentElement.style;
}

/** 外观设置面板：界面字体 / 界面字号 / 代码字体，实时预览并即时生效，偏好仅保存在本机。 */
export function AppearanceSettingsPanel({ disabled }: SettingsCustomItemProps) {
  const { t } = useTranslation();
  const [prefs, setPrefs] = useState<AppearancePreferences>(() => readAppearancePreferences(browserLocalStorage()));
  // 自定义字体输入采用「失焦 / 回车提交」草稿，避免逐键切换全局字体导致闪烁
  const [uiFontCustomDraft, setUiFontCustomDraft] = useState<string | null>(null);
  const [codeFontCustomDraft, setCodeFontCustomDraft] = useState<string | null>(null);

  const update = (next: AppearancePreferences): void => {
    setPrefs(next);
    saveAppearancePreferences(browserLocalStorage(), next);
    applyAppearancePreferences(next, rootStyleTarget());
  };

  const commitUiFontCustom = (): void => {
    if (uiFontCustomDraft === null) return;
    update({ ...prefs, uiFontCustom: uiFontCustomDraft });
    setUiFontCustomDraft(null);
  };

  const commitCodeFontCustom = (): void => {
    if (codeFontCustomDraft === null) return;
    update({ ...prefs, codeFontCustom: codeFontCustomDraft });
    setCodeFontCustomDraft(null);
  };

  const handleReset = (): void => {
    clearAppearancePreferences(browserLocalStorage());
    setUiFontCustomDraft(null);
    setCodeFontCustomDraft(null);
    setPrefs(defaultAppearancePreferences);
    applyAppearancePreferences(defaultAppearancePreferences, rootStyleTarget());
  };

  const uiFontOptions: SelectOption[] = [
    { value: 'default', label: t('settingsPanel.appearance.uiFontDefault') },
    { value: 'zh-hei', label: t('settingsPanel.appearance.uiFontZhHei') },
    { value: 'zh-song', label: t('settingsPanel.appearance.uiFontZhSong') },
    { value: 'zh-kai', label: t('settingsPanel.appearance.uiFontZhKai') },
    { value: 'custom', label: t('settingsPanel.appearance.uiFontCustom') },
  ];

  const fontSizeOptions: SelectOption[] = FONT_SCALE_STEPS.map((step) => ({
    value: String(step),
    label: t(FONT_SCALE_LABEL_KEYS[step] ?? 'settingsPanel.appearance.fontSizeDefault'),
  }));

  const codeFontOptions: SelectOption[] = [
    { value: 'default', label: t('settingsPanel.appearance.codeFontDefault') },
    { value: 'jetbrains-fira', label: t('settingsPanel.appearance.codeFontJetbrainsFira') },
    { value: 'cascadia-code', label: t('settingsPanel.appearance.codeFontCascadia') },
    { value: 'custom', label: t('settingsPanel.appearance.codeFontCustom') },
  ];

  return (
    <>
      <SettingRow
        title={t('settingsPanel.appearance.uiFont')}
        description={t('settingsPanel.appearance.uiFontDescription')}
        subSettings={
          prefs.uiFontPreset === 'custom' ? (
            <div className="appearance-panel__custom">
              <Input
                value={uiFontCustomDraft ?? prefs.uiFontCustom}
                placeholder={t('settingsPanel.appearance.customFontPlaceholder')}
                onChange={(value) => setUiFontCustomDraft(value)}
                onBlur={commitUiFontCustom}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') commitUiFontCustom();
                }}
                disabled={disabled}
                data-testid="appearance-ui-font-custom-input"
              />
              <p className="appearance-panel__custom-hint">{t('settingsPanel.appearance.customFontHint')}</p>
            </div>
          ) : null
        }
      >
        <Select
          value={prefs.uiFontPreset}
          options={uiFontOptions}
          onChange={(value) => update({ ...prefs, uiFontPreset: value })}
          disabled={disabled}
          data-testid="appearance-ui-font-select"
        />
      </SettingRow>

      <SettingRow
        title={t('settingsPanel.appearance.fontSize')}
        description={t('settingsPanel.appearance.fontSizeDescription')}
      >
        <Select
          value={String(nearestFontScaleStep(prefs.fontScale))}
          options={fontSizeOptions}
          onChange={(value) => update({ ...prefs, fontScale: Number(value) })}
          disabled={disabled}
          data-testid="appearance-font-size-select"
        />
      </SettingRow>

      <SettingRow
        title={t('settingsPanel.appearance.codeFont')}
        description={t('settingsPanel.appearance.codeFontDescription')}
        subSettings={
          prefs.codeFontPreset === 'custom' ? (
            <div className="appearance-panel__custom">
              <Input
                value={codeFontCustomDraft ?? prefs.codeFontCustom}
                placeholder={t('settingsPanel.appearance.customFontPlaceholder')}
                onChange={(value) => setCodeFontCustomDraft(value)}
                onBlur={commitCodeFontCustom}
                onKeyDown={(event) => {
                  if (event.key === 'Enter') commitCodeFontCustom();
                }}
                disabled={disabled}
                data-testid="appearance-code-font-custom-input"
              />
              <p className="appearance-panel__custom-hint">{t('settingsPanel.appearance.customFontHint')}</p>
            </div>
          ) : null
        }
      >
        <Select
          value={prefs.codeFontPreset}
          options={codeFontOptions}
          onChange={(value) => update({ ...prefs, codeFontPreset: value })}
          disabled={disabled}
          data-testid="appearance-code-font-select"
        />
      </SettingRow>

      <div className="appearance-panel__preview" data-testid="appearance-preview">
        <span className="appearance-panel__preview-label">{t('settingsPanel.appearance.preview')}</span>
        <p className="appearance-panel__preview-body">{t('settingsPanel.appearance.previewBody')}</p>
        <code className="appearance-panel__preview-code">{t('settingsPanel.appearance.previewCodeSample')}</code>
      </div>

      <SettingRow
        title={t('settingsPanel.appearance.resetTitle')}
        description={t('settingsPanel.appearance.resetDescription')}
      >
        <Button onClick={handleReset} disabled={disabled} data-testid="appearance-reset-button">
          {t('settingsPanel.appearance.resetButton')}
        </Button>
      </SettingRow>
    </>
  );
}

/** 字号选择框显示值：把历史保存的非标准档位吸附到最近的可选档位。 */
function nearestFontScaleStep(scale: number): number {
  let nearest: (typeof FONT_SCALE_STEPS)[number] = FONT_SCALE_STEPS[0];
  for (const step of FONT_SCALE_STEPS) {
    if (Math.abs(step - scale) < Math.abs(nearest - scale)) nearest = step;
  }
  return nearest;
}
