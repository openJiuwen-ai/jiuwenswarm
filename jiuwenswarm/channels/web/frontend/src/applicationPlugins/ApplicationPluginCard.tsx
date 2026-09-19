import { useId, useState } from 'react';
import { Boxes } from 'lucide-react';
import { useTranslation } from 'react-i18next';
import type { ComponentType } from 'react';

import type { ApplicationPluginContribution, ApplicationPluginSettingsProps } from './types';

/**
 * One plugin's card on the Application plugins page.
 *
 * Identity comes from the plugin itself: the icon is a data URI the server
 * inlined out of the plugin's own directory, and the description is whatever
 * the plugin's ``description_i18n_key`` resolves to. A plugin that ships
 * neither keeps the generic glyph and the card it always had.
 *
 * Its own module, and it takes the settings component as a prop rather than
 * reaching for the outlet: the outlet resolves bundled plugins through Vite's
 * ``import.meta.glob``, which nothing outside a Vite build can evaluate, and
 * that would make this card untestable.
 */
export function ApplicationPluginCard({
  plugin,
  settings: Settings,
  onRefresh,
}: {
  plugin: ApplicationPluginContribution;
  settings?: ComponentType<ApplicationPluginSettingsProps>;
  onRefresh: () => Promise<void>;
}) {
  const { t } = useTranslation();
  const [descriptionOpen, setDescriptionOpen] = useState(false);
  const descriptionId = useId();

  const title = plugin.title_i18n_key ? t(plugin.title_i18n_key, plugin.title) : plugin.title;
  // The plugin's own name. Falls back to the id, which is at least an identifier
  // someone can match against a log line, rather than repeating the title.
  const subtitle = plugin.name_i18n_key ? t(plugin.name_i18n_key, plugin.plugin_id) : plugin.plugin_id;
  // The localized string wins; the plugin's own extension.yaml description is
  // the fallback, so a plugin that ships no i18n key still says something.
  const description = plugin.description_i18n_key
    ? t(plugin.description_i18n_key, plugin.description ?? '')
    : plugin.description ?? '';
  // The card shows the plugin's own mark. `logo` is the identity artwork; `icon`
  // is the rail's monochrome mark, used here only when a plugin ships no logo.
  const mark = plugin.logo || plugin.icon;
  const glyph = mark
    ? <img src={mark} alt="" width={28} height={28} />
    : <Boxes aria-hidden />;
  // The muted plate exists to anchor the fallback glyph, which is a bare line
  // drawing. A plugin's own artwork already carries its shape, and the plate
  // reads as a grey frame around it.
  const iconClass = `application-plugins-panel__icon${mark ? ' application-plugins-panel__icon--art' : ''}`;
  const toggleLabel = t(
    descriptionOpen ? 'applicationPlugins.hideDescription' : 'applicationPlugins.showDescription',
  );

  return (
    <article className="application-plugins-panel__item">
      <div className="application-plugins-panel__identity">
        {description ? (
          <button
            type="button"
            className={`${iconClass} application-plugins-panel__icon--button`}
            onClick={() => setDescriptionOpen(open => !open)}
            aria-expanded={descriptionOpen}
            aria-controls={descriptionId}
            aria-label={toggleLabel}
            title={toggleLabel}
            data-testid="application-plugin-icon"
          >
            {glyph}
          </button>
        ) : (
          <span className={iconClass} data-testid="application-plugin-icon">
            {glyph}
          </span>
        )}
        <div>
          <strong>{title}</strong>
          {/* The plugin's own name, not its id: the card is read by a person
              deciding whether to keep the thing, and `co-scribe · v1.0.0` tells
              them nothing they can act on. The id stays in the tooltip for
              anyone who needs to match it against a log line. */}
          <span title={`${plugin.plugin_id} · v${plugin.plugin_version}`}>{subtitle}</span>
        </div>
        <span className={`application-plugins-panel__status${plugin.enabled !== false ? ' is-enabled' : ''}`}>
          {plugin.enabled !== false ? t('applicationPlugins.enabled') : t('applicationPlugins.disabled')}
        </span>
      </div>
      {description && (
        <p
          id={descriptionId}
          className="application-plugins-panel__description"
          hidden={!descriptionOpen}
          data-testid="application-plugin-description"
        >
          {description}
        </p>
      )}
      {Settings ? (
        <Settings contribution={plugin} onManifestChanged={() => void onRefresh()} />
      ) : (
        <p className="application-plugins-panel__no-settings">{t('applicationPlugins.noSettings')}</p>
      )}
    </article>
  );
}
