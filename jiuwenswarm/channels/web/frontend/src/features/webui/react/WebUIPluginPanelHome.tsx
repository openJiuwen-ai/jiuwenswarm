/**
 * Blank plugin tab content: lists the plugins registered on `conversation.panel.right` and turns
 * the hosting blank tab into the clicked plugin tab.
 *
 * The tab itself is plugin-agnostic and closable; choosing a plugin replaces its content in place.
 * Plugins declare nothing extra: the title and instance identity come from the same discovery
 * result that used to create the tabs automatically.
 */

import { useTranslation } from 'react-i18next';
import { EmptyState } from '../../../components/ui/EmptyState/EmptyState';
import type { WebUIPanelEntry } from './useWebUIPanels.tsx';
import { webUIPluginPanelIcon } from './webuiPanelIcon.tsx';
import './WebUIPluginPanelHome.css';

export interface WebUIPluginPanelHomeProps {
  panels: readonly WebUIPanelEntry[];
  openPanelIds: readonly string[];
  onOpenPanel: (panelId: string) => void;
}

export function WebUIPluginPanelHome({ panels, openPanelIds, onOpenPanel }: WebUIPluginPanelHomeProps) {
  const { t } = useTranslation();

  return (
    <div className="webui-panel-home" data-testid="webui-panel-home">
      <div className="webui-panel-home__intro">
        <h2 className="webui-panel-home__title" data-testid="webui-panel-home-title">
          {t('toolPanel.plugins.title')}
        </h2>
        <p className="webui-panel-home__description" data-testid="webui-panel-home-description">
          {t('toolPanel.plugins.description')}
        </p>
      </div>
      {panels.length === 0 ? (
        <EmptyState id="webui-panel-home-empty" text={t('toolPanel.plugins.empty')} />
      ) : (
        <ul className="webui-panel-home__list" data-testid="webui-panel-home-list">
          {panels.map((panel) => {
            const opened = openPanelIds.includes(panel.panelId);
            return (
              <li key={panel.panelId}>
                <button
                  type="button"
                  className="webui-panel-home__item"
                  data-testid="webui-panel-home-item"
                  data-variant={panel.panelId}
                  onClick={() => onOpenPanel(panel.panelId)}
                >
                  <span className="webui-panel-home__icon" aria-hidden="true">
                    {webUIPluginPanelIcon(panel)}
                  </span>
                  <span className="webui-panel-home__name" title={panel.title}>{panel.title}</span>
                  {opened ? (
                    <span className="webui-panel-home__badge" data-testid="webui-panel-home-item-added">
                      {t('toolPanel.plugins.added')}
                    </span>
                  ) : (
                    <span className="webui-panel-home__action" data-testid="webui-panel-home-item-add">
                      {t('toolPanel.plugins.addTab')}
                    </span>
                  )}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
