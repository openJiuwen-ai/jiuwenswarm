import type { TFunction } from 'i18next';
import { useTranslation } from 'react-i18next';
import { Button } from '../../../../components/ui';
import { useMultimodalSettings } from '../../services/useMultimodalSettings';
import { multimodalErrorDetails, type MultimodalCapability } from '../../services/multimodalSettings';

export function multimodalErrorMessage(error: unknown, t: TFunction): string {
  const { code } = multimodalErrorDetails(error);
  return t(`settingsPanel.multimodal.errors.${code ?? 'unknown'}`, { defaultValue: t('settingsPanel.feedback.saveFailed') });
}

export function MultimodalReferenceMessage({ tools, operation }: { tools: MultimodalCapability[]; operation: 'edit' | 'delete' }) {
  const { t } = useTranslation();
  return <div data-testid="settings-multimodal-reference-message">
    {tools.length > 0 ? <p data-testid="settings-multimodal-reference-tools">{t(`settingsPanel.multimodal.${operation}References`, {
      tools: tools.map((tool) => t(`settingsPanel.multimodal.tools.${tool}`)).join(t('settingsPanel.multimodal.separator')),
    })}</p> : null}
    <p data-testid="settings-multimodal-reference-rule">{t(tools.length ? 'settingsPanel.multimodal.replacementRule' : 'settingsPanel.multimodal.deleteUnbound')}</p>
  </div>;
}

export function MultimodalSettingsFeedback() {
  const { t } = useTranslation();
  const { error, saving, store, snapshot, lastChange, notice } = useMultimodalSettings();
  const code = multimodalErrorDetails(error).code;
  const visibleError = error && code !== 'MM_CONFIRM_REQUIRED';
  const unapplied = lastChange && !lastChange.applied_without_restart;
  if (!visibleError && !unapplied && !notice) return null;
  return <div className={visibleError || unapplied ? 'settings-page__error' : 'settings-page__notice'}
    role={visibleError || unapplied ? 'alert' : 'status'} data-testid="settings-multimodal-feedback">
    {visibleError ? multimodalErrorMessage(error, t) : unapplied ? t('settingsPanel.multimodal.applyFailed') : null}
    {notice ? <p data-testid="settings-multimodal-shortcut-notice">{t('settingsPanel.multimodal.shortcutChanged', { tool: t(`settingsPanel.multimodal.tools.${notice.tool}`) })}</p> : null}
    {visibleError || unapplied ? <Button disabled={saving} onClick={() => {
      if (unapplied && snapshot) void store.write('config.apply', {}).catch(() => undefined);
      else void store.load(true);
    }} data-testid="settings-multimodal-retry-btn">{t('settingsPanel.feedback.retry')}</Button> : null}
  </div>;
}
