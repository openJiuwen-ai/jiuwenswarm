import { useEffect, useId, useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';

import { X } from 'lucide-react';

import { Button, Dialog } from '../../../../components/ui';
import type { ModelEntry } from '../../../../types';
import { useSessionStore } from '../../../../stores/sessionStore';
import { useSettingsServices } from '../../services/SettingsServicesProvider';
import { ContextWindowField } from './ContextWindowField';
import { normalizeContextWindowTokens, parseContextWindowTokens } from './contextWindow';
import { parseModelsPayload } from './ModelsSettings';
import './FreeModelSettingsDialog.css';

export function FreeModelSettingsDialog({
  open,
  models,
  onClose,
}: {
  open: boolean;
  models: ModelEntry[];
  onClose: () => void;
}) {
  const { t } = useTranslation();
  const titleId = useId();
  const { isConnected, request, saveQueue } = useSettingsServices();
  const setAvailableModels = useSessionStore((state) => state.setAvailableModels);
  const [drafts, setDrafts] = useState<Record<string, string>>({});
  const [error, setError] = useState('');
  const [saving, setSaving] = useState(false);

  useEffect(() => {
    if (open) {
      setDrafts({});
      setError('');
    }
  }, [open]);

  const valueOf = (model: ModelEntry) =>
    drafts[model.model_name] ?? normalizeContextWindowTokens(model.context_window_tokens);

  const hasInvalid = useMemo(
    () =>
      models.some(
        (model) =>
          parseContextWindowTokens(
            drafts[model.model_name] ?? normalizeContextWindowTokens(model.context_window_tokens),
          ) === null,
      ),
    [drafts, models],
  );

  const save = async () => {
    const changes: Record<string, { context_window: number }> = {};
    for (const model of models) {
      const draft = drafts[model.model_name];
      if (draft === undefined) continue;
      const tokens = parseContextWindowTokens(draft);
      if (tokens === null) {
        setError(t('settingsPanel.models.validation.contextWindowInvalid'));
        return;
      }
      changes[model.model_name] = { context_window: tokens };
    }
    if (Object.keys(changes).length === 0) {
      onClose();
      return;
    }
    setSaving(true);
    setError('');
    try {
      await saveQueue.enqueue('free_models.context_window', async () => {
        await request('config.save_all', { login_model_settings: changes }, { timeoutMs: 600_000 });
        const parsed = parseModelsPayload(await request('models.list'));
        setAvailableModels(parsed.models, parsed.activeModel);
      });
      onClose();
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : t('settingsPanel.feedback.saveFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <Dialog open={open} titleId={titleId} className="free-models-config-dialog" closeDisabled={saving} onCancel={onClose}>
      <div className="free-models-config">
        <header className="free-models-config__header">
          <h2 id={titleId}>{t('settingsPanel.freeModels.configTitle')}</h2>
          <button
            type="button"
            className="free-models-config__close"
            aria-label={t('common.close')}
            title={t('common.close')}
            disabled={saving}
            onClick={onClose}
          >
            <X aria-hidden />
          </button>
        </header>
        <div className="free-models-config__body">
          <section className="free-models-config__group">
            <h3 className="free-models-config__group-title">{t('settingsPanel.models.contextWindow')}</h3>
            <div className="free-models-config__box">
              {models.map((model) => {
                const name = model.model_name;
                const inputId = `${titleId}-${name}`;
                const value = valueOf(model);
                const invalid = parseContextWindowTokens(value) === null;
                return (
                  <div className="free-models-config__item" key={name}>
                    <label className="free-models-config__name" htmlFor={inputId} title={name}>
                      {model.alias || name}
                    </label>
                    <ContextWindowField
                      id={inputId}
                      value={value}
                      error={invalid ? ' ' : undefined}
                      disabled={!isConnected || saving}
                      placeholder={t('settingsPanel.models.contextWindowPlaceholder')}
                      presetLabel={t('settingsPanel.models.contextWindowPresets')}
                      onChange={(next) => setDrafts((current) => ({ ...current, [name]: next }))}
                      onBlur={() => undefined}
                    />
                  </div>
                );
              })}
            </div>
            <p className="free-models-config__hint">{t('settingsPanel.models.contextWindowHint')}</p>
          </section>
          {error ? (
            <div className="free-models-config__error" role="alert">
              {error}
            </div>
          ) : null}
        </div>
        <footer className="free-models-config__footer">
          <Button size="sm" disabled={saving} onClick={onClose}>
            {t('common.cancel')}
          </Button>
          <Button variant="primary" size="sm" loading={saving} disabled={hasInvalid} onClick={() => void save()}>
            {t('common.save')}
          </Button>
        </footer>
      </div>
    </Dialog>
  );
}
