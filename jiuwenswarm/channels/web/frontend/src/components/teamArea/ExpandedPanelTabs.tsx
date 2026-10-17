import { useId, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import CollapseAllIcon from '../../assets/collapse-all.svg?react';
import ExpandAllIcon from '../../assets/expand-all.svg?react';
import PanelCollapseIcon from '../../assets/panel-collapse.svg?react';
import RecentTasksIcon from '../../assets/work-mode/recent-tasks.svg?react';
import WebsiteBuildIcon from '../../assets/work-mode/website-build.svg?react';
import artifactsIcon from '../../assets/artifacts.svg';
import reviewIcon from '../../assets/review.svg';
import { CloseButton } from '../ui';
import '../subagent/Subagent.css';
import '../ChatPanel/ChatPanel.css';

export interface PanelTabItem {
  key: string;
  label: string;
  icon?: ReactNode;
  count?: string | number;
  /** 页签文字右侧渲染关闭按钮（当前仅 browser 页签使用） */
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
        {tabs.map((tab) => {
          const isActive = activeTab === tab.key;
          const countSuffix = tab.count !== undefined ? ` (${tab.count})` : '';
          return (
            <div
              key={tab.key}
              data-testid={`${testIdPrefix}-tab`}
              data-variant={tab.key}
              id={`${tabPanelId}-${tab.key}`}
              role="tab"
              tabIndex={0}
              aria-selected={isActive}
              aria-controls={`${tabPanelId}-panel`}
              className={`single-agent-tool-tab group ${isActive ? 'single-agent-tool-tab--active' : ''}`}
              onClick={() => onTabChange(tab.key)}
              onKeyDown={(event) => {
                // 仅响应标签自身聚焦时的 Enter/Space；事件若来自内部关闭按钮（会冒泡），
                // 不能 preventDefault——否则按钮原生 click 不触发，关闭失效反而切页
                if (event.target !== event.currentTarget) return;
                if (event.key !== 'Enter' && event.key !== ' ') return;
                event.preventDefault();
                onTabChange(tab.key);
              }}
            >
              {tab.icon}
              {tab.label}
              {countSuffix}
              {tab.closable && onTabClose && (
                <span
                  data-variant={tab.key}
                  className="ml-1 -mr-0.5 inline-flex opacity-50 transition-opacity group-hover:opacity-100 focus-within:opacity-100"
                  onClick={(event) => event.stopPropagation()}
                >
                  <CloseButton
                    size={16}
                    testId={`${testIdPrefix}-tab-close`}
                    ariaLabel={t('browser.pane.closeTab')}
                    title={t('browser.pane.closeTab')}
                    onClick={() => onTabClose(tab.key)}
                  />
                </span>
              )}
            </div>
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
