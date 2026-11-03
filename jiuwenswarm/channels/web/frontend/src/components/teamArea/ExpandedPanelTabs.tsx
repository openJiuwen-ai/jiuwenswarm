import { useId, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import CollapseAllIcon from '../../assets/collapse-all.svg?react';
import ExpandAllIcon from '../../assets/expand-all.svg?react';
import PanelCollapseIcon from '../../assets/panel-collapse.svg?react';
import RecentTasksIcon from '../../assets/work-mode/recent-tasks.svg?react';
import WebsiteBuildIcon from '../../assets/work-mode/website-build.svg?react';
import artifactsIcon from '../../assets/artifacts.svg';
import reviewIcon from '../../assets/review.svg';
import { CloseButton } from '../ui/CloseButton/CloseButton';
import './ExpandedPanelTabs.css';

export interface PanelTabItem {
  key: string;
  label: string;
  icon?: ReactNode;
  count?: string | number;
  /** Close button renders when the section bar provides onTabClose; plugin pages are always closable. */
  closable?: boolean;
}

export function useExpandedPanelTabs({
  middleTab,
  showMiddleTab,
  artifactsCount,
  reviewPanel,
  showBrowserTab,
}: {
  middleTab: { key: string; label: string; icon: ReactNode };
  showMiddleTab: boolean;
  artifactsCount: number;
  reviewPanel?: ReactNode;
  showBrowserTab?: boolean;
}): PanelTabItem[] {
  const { t } = useTranslation();
  return [
    {
      key: 'planning',
      label: t('team.planning.tab'),
      icon: <RecentTasksIcon className="h-4 w-4" aria-hidden="true" />,
    },
    ...(showMiddleTab ? [middleTab] : []),
    ...(artifactsCount > 0
      ? [
          {
            key: 'artifacts',
            label: t('artifacts.tab'),
            icon: <img src={artifactsIcon} width={16} height={16} aria-hidden="true" />,
          },
        ]
      : []),
    ...(reviewPanel
      ? [
          {
            key: 'review',
            label: t('codeMode.review'),
            icon: <img src={reviewIcon} width={16} height={16} aria-hidden="true" />,
          },
        ]
      : []),
    ...(showBrowserTab
      ? [
          {
            key: 'browser',
            label: t('browser.pane.tabLabel'),
            icon: <WebsiteBuildIcon className="h-4 w-4" aria-hidden="true" />,
            closable: true,
          },
        ]
      : []),
  ];
}

export function ExpandedPanelTabs({
  tabs,
  activeTab,
  onTabChange,
  onTabClose,
  onCollapse,
  onToggleFullscreen,
  isFullscreen,
  testIdPrefix = 'tool-panel',
  tabListLabel,
  idPrefix,
  variant = 'pages',
}: {
  tabs: PanelTabItem[];
  activeTab: string;
  onTabChange: (tab: string) => void;
  onTabClose?: (tab: string) => void;
  onCollapse?: () => void;
  onToggleFullscreen?: () => void;
  isFullscreen?: boolean;
  testIdPrefix?: string;
  tabListLabel?: string;
  idPrefix?: string;
  variant?: 'pages' | 'sections';
}) {
  const { t } = useTranslation();
  const generatedId = useId();
  const tabPanelId = idPrefix ?? generatedId;

  return (
    <div data-testid={`${testIdPrefix}-expanded-header`} className={`panel-pages-header panel-pages-header--${variant}`}>
      <div
        data-testid={`${testIdPrefix}-expanded-tabs`}
        className="panel-pages-list"
        role="tablist"
        aria-label={tabListLabel ?? t('team.toolTabs')}
      >
        {tabs.map((tab, index) => {
          const isActive = activeTab === tab.key;
          const countSuffix = tab.count !== undefined ? ` (${tab.count})` : '';
          return (
            <div key={tab.key} className={`panel-page ${isActive ? 'panel-page--active' : ''}`} role="presentation">
              <button
                type="button"
                data-testid={`${testIdPrefix}-tab`}
                data-variant={tab.key}
                id={`${tabPanelId}-${encodeURIComponent(tab.key)}`}
                role="tab"
                tabIndex={isActive ? 0 : -1}
                aria-selected={isActive}
                aria-controls={`${tabPanelId}-panel`}
                title={tab.label}
                className="panel-page-select"
                onClick={() => onTabChange(tab.key)}
                onKeyDown={(event) => {
                  const nextIndex = event.key === 'ArrowRight' ? (index + 1) % tabs.length
                    : event.key === 'ArrowLeft' ? (index + tabs.length - 1) % tabs.length
                    : event.key === 'Home' ? 0 : event.key === 'End' ? tabs.length - 1 : null;
                  if (nextIndex === null) return;
                  event.preventDefault();
                  const next = tabs[nextIndex];
                  onTabChange(next.key);
                  document.getElementById(`${tabPanelId}-${encodeURIComponent(next.key)}`)?.focus();
                }}
              >
                {tab.icon}
                <span className="panel-page-label">{tab.label}</span>
                {countSuffix}
              </button>
              {tab.closable && onTabClose && (
                <span
                  data-variant={tab.key}
                  className="panel-page-close"
                >
                  <CloseButton
                    size={20}
                    testId={`${testIdPrefix}-tab-close`}
                    ariaLabel={t('team.closeToolTab', { title: tab.label })}
                    title={t('team.closeToolTab', { title: tab.label })}
                    onClick={() => {
                      onTabClose(tab.key);
                      const next = isActive ? tabs[index + 1] ?? tabs[index - 1]
                        : tabs.find((item) => item.key === activeTab);
                      if (next) document.getElementById(`${tabPanelId}-${encodeURIComponent(next.key)}`)?.focus();
                    }}
                  />
                </span>
              )}
            </div>
          );
        })}
      </div>

      <div className="panel-pages-controls">
        {onToggleFullscreen && (
          <button
            onClick={onToggleFullscreen}
            data-testid={`${testIdPrefix}-maximize`}
            type="button"
            className="panel-pages-control"
            aria-label={isFullscreen ? t('team.restore') : t('team.maximize')}
            title={isFullscreen ? t('team.restore') : t('team.maximize')}
          >
            {isFullscreen ? (
              <CollapseAllIcon className="h-[21.33px] w-[21.33px]" aria-hidden="true" />
            ) : (
              <ExpandAllIcon className="h-[21.33px] w-[21.33px]" aria-hidden="true" />
            )}
          </button>
        )}
        {onCollapse && (
          <button
            onClick={onCollapse}
            data-testid={`${testIdPrefix}-collapse`}
            type="button"
            className="panel-pages-control"
            aria-label={t('team.collapse')}
            title={t('team.collapse')}
          >
            <PanelCollapseIcon className="h-[32px] w-[32px]" aria-hidden="true" />
          </button>
        )}
      </div>
    </div>
  );
}
