import { ArrowLeft, Boxes, LoaderCircle, RefreshCw } from 'lucide-react';
import { useTranslation } from 'react-i18next';

import { applicationPluginSettingsComponent } from './ApplicationPluginOutlet';
import type { ApplicationPluginContribution } from './types';
import './applicationPlugins.css';

export function ApplicationPluginsPanel({
  plugins,
  loading,
  error,
  onRefresh,
  onBack,
}: {
  plugins: ApplicationPluginContribution[];
  loading: boolean;
  error: string;
  onRefresh: () => Promise<void>;
  onBack?: () => void;
}) {
  const { t } = useTranslation();
  const uniquePlugins = plugins.filter(
    (plugin, index) => plugins.findIndex(candidate => candidate.plugin_id === plugin.plugin_id) === index,
  );

  return (
    <section className="application-plugins-panel">
      <header className="application-plugins-panel__header">
        <div className="application-plugins-panel__heading">
          {onBack && (
            <button type="button" onClick={onBack} title={t('applicationPlugins.backToExtensions')}>
              <ArrowLeft aria-hidden />
            </button>
          )}
          <div>
            <h1>{t('applicationPlugins.title')}</h1>
            <p>{t('applicationPlugins.description')}</p>
          </div>
        </div>
        <button type="button" onClick={() => void onRefresh()} disabled={loading} title={t('applicationPlugins.refresh')}>
          {loading ? <LoaderCircle className="is-spinning" aria-hidden /> : <RefreshCw aria-hidden />}
          {t('applicationPlugins.refresh')}
        </button>
      </header>

      {error && <div className="application-plugins-panel__error">{error}</div>}
      {!loading && uniquePlugins.length === 0 && (
        <div className="application-plugins-panel__empty">
          <Boxes aria-hidden />
          <span>{t('applicationPlugins.empty')}</span>
        </div>
      )}
      <div className="application-plugins-panel__list">
        {uniquePlugins.map(plugin => {
          const Settings = applicationPluginSettingsComponent(plugin.plugin_id);
          return (
            <article className="application-plugins-panel__item" key={plugin.plugin_id}>
              <div className="application-plugins-panel__identity">
                <span className="application-plugins-panel__icon"><Boxes aria-hidden /></span>
                <div>
                  <strong>{plugin.title_i18n_key ? t(plugin.title_i18n_key, plugin.title) : plugin.title}</strong>
                  <span>{plugin.plugin_id} · v{plugin.plugin_version}</span>
                </div>
                <span className={`application-plugins-panel__status${plugin.enabled !== false ? ' is-enabled' : ''}`}>
                  {plugin.enabled !== false ? t('applicationPlugins.enabled') : t('applicationPlugins.disabled')}
                </span>
              </div>
              {Settings ? (
                <Settings contribution={plugin} onManifestChanged={() => void onRefresh()} />
              ) : (
                <p className="application-plugins-panel__no-settings">{t('applicationPlugins.noSettings')}</p>
              )}
            </article>
          );
        })}
      </div>
    </section>
  );
}
