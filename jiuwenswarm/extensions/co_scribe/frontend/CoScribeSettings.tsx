/**
 * The co-scribe card on the 应用插件 page.
 *
 * One switch and one hint. The switch is ``clouddoc.enabled`` -- the same
 * flag the plugin's ``is_enabled()`` reads, which is what decides whether the
 * Docs nav item is contributed at all, so the badge on this card, the rail and
 * the feature itself cannot drift apart. Everything else co-scribe owns --
 * connections, keys, managed documents, watches -- is managed on the Docs page
 * and is deliberately not rebuilt here.
 */
import { useEffect, useState } from 'react';
import { LoaderCircle, Power } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import type { ApplicationPluginSettingsProps } from '../../../channels/web/frontend/src/applicationPlugins/types';
import { webRequest } from '../../../channels/web/frontend/src/services/webClient';
import './CoScribeSettings.css';

interface SettingsPayload {
  enabled: boolean;
}

export function CoScribeSettings({ contribution, onManifestChanged }: ApplicationPluginSettingsProps) {
  const { t } = useTranslation();
  const [enabled, setEnabled] = useState(contribution.enabled !== false);
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState('');

  useEffect(() => {
    let active = true;
    setLoading(true);
    void webRequest<SettingsPayload>('co_scribe.settings.get', {}, { timeoutMs: 10_000 })
      .then(payload => {
        if (active) setEnabled(payload.enabled);
      })
      .catch((loadError: unknown) => {
        if (active) setError(loadError instanceof Error ? loadError.message : t('docs.plugin.loadFailed'));
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => { active = false; };
  }, [t]);

  const toggleEnabled = async () => {
    if (saving || loading) return;
    setSaving(true);
    setError('');
    try {
      const payload = await webRequest<SettingsPayload>(
        'co_scribe.settings.set_enabled',
        { enabled: !enabled },
        { timeoutMs: 10_000 },
      );
      setEnabled(payload.enabled);
      // The manifest is the one place the rest of the app reads this flag from,
      // so a successful write has to be followed by a re-read, not by a second
      // copy of the answer kept here.
      onManifestChanged();
    } catch (toggleError) {
      setError(toggleError instanceof Error ? toggleError.message : t('docs.plugin.toggleFailed'));
    } finally {
      setSaving(false);
    }
  };

  return (
    <div className="co-scribe-settings">
      <div>
        {/* The card's own description disclosure renders docs.plugin.summary --
            the contribution points its description_i18n_key at it -- so the
            settings block would only say the same sentence twice. */}
        <small>{t('docs.plugin.manageHint')}</small>
        {error && <small className="is-error">{error}</small>}
      </div>
      <button
        type="button"
        className={enabled ? 'is-danger' : ''}
        disabled={loading || saving}
        onClick={() => void toggleEnabled()}
        data-testid="co-scribe-plugin-toggle"
        data-variant={enabled ? 'disable' : 'enable'}
      >
        {loading || saving ? <LoaderCircle className="is-spinning" aria-hidden /> : <Power aria-hidden />}
        {enabled ? t('docs.plugin.disable') : t('docs.plugin.enable')}
      </button>
    </div>
  );
}
