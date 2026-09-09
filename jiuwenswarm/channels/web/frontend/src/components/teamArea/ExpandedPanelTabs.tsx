import { useId, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Minimize2 } from 'lucide-react';
import MaximizeIcon from '../../assets/maximize.svg?react';
import PanelCollapseIcon from '../../assets/panel-collapse.svg?react';
import RecentTasksIcon from '../../assets/work-mode/progress-tasks.svg?react';
import artifactsIcon from '../../assets/artifacts.svg';
import reviewIcon from '../../assets/review.svg';
import '../subagent/Subagent.css';
import '../ChatPanel/ChatPanel.css';

export interface PanelTabItem {
  key: string;
  label: string;
  icon?: ReactNode;
  count?: string | number;
}

export function useExpandedPanelTabs({
  middleTab,
  showMiddleTab,
  artifactsCount,
  reviewPanel,
}: {
  middleTab: { key: string; label: string; icon: ReactNode };
  showMiddleTab: boolean;
  artifactsCount: number;
  reviewPanel?: ReactNode;
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
    ...(reviewPanel ? [{ key: 'review', label: t('codeMode.review'), icon: <img src={reviewIcon} width={16} height={16} aria-hidden="true" /> }] : []),
  ];
}

export function ExpandedPanelTabs({
  tabs,
  activeTab,
  onTabChange,
  onCollapse,
  onToggleFullscreen,
  isFullscreen,
  testIdPrefix = 'tool-panel',
  tabListLabel,
}: {
  tabs: PanelTabItem[];
  activeTab: string;
  onTabChange: (tab: string) => void;
  onCollapse?: () => void;
  onToggleFullscreen?: () => void;
  isFullscreen?: boolean;
  testIdPrefix?: string;
  tabListLabel?: string;
}) {
  const { t } = useTranslation();
  const tabPanelId = useId();

  return (
    <div data-testid={`${testIdPrefix}-expanded-header`} className="single-agent-tool-tabs">
      <div
        data-testid={`${testIdPrefix}-expanded-tabs`}
        className="single-agent-tool-tabs__list"
        role="tablist"
        aria-label={tabListLabel ?? t('team.toolTabs')}
      >
        {tabs.map(tab => {
          const isActive = activeTab === tab.key;
          const countSuffix = tab.count !== undefined ? ` (${tab.count})` : '';
          return (
            <button
              key={tab.key}
              data-testid={`${testIdPrefix}-tab`}
              data-variant={tab.key}
              id={`${tabPanelId}-${tab.key}`}
              type="button"
              role="tab"
              aria-selected={isActive}
              aria-controls={`${tabPanelId}-panel`}
              className={`single-agent-tool-tab ${isActive ? 'single-agent-tool-tab--active' : ''}`}
              onClick={() => onTabChange(tab.key)}
            >
              {tab.icon}
              {tab.label}
              {countSuffix}
            </button>
          );
        })}
      </div>

      <div className="flex items-center gap-2">
        {onToggleFullscreen && (
          <button
            onClick={onToggleFullscreen}
            data-testid={`${testIdPrefix}-maximize`}
            className="chat-header-icon-btn panel-tab-icon-btn"
            aria-label={isFullscreen ? t('team.restore') : t('team.maximize')}
            title={isFullscreen ? t('team.restore') : t('team.maximize')}
          >
            {isFullscreen ? <Minimize2 className="h-[21.33px] w-[21.33px]" /> : <MaximizeIcon className="h-[21.33px] w-[21.33px]" aria-hidden="true" />}
          </button>
        )}
        {onCollapse && (
          <button
            onClick={onCollapse}
            data-testid={`${testIdPrefix}-collapse`}
            className="chat-header-icon-btn panel-tab-icon-btn panel-tab-icon-btn--collapse"
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
