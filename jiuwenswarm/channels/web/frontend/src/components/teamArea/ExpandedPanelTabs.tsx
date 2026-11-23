import { useEffect, useId, useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';
import { useTranslation } from 'react-i18next';
import { Plus } from 'lucide-react';
import CollapseAllIcon from '../../assets/collapse-all.svg?react';
import ExpandAllIcon from '../../assets/expand-all.svg?react';
import PanelCollapseIcon from '../../assets/panel-collapse.svg?react';
import RecentTasksIcon from '../../assets/work-mode/recent-tasks.svg?react';
import WebsiteBuildIcon from '../../assets/work-mode/website-build.svg?react';
import artifactsIcon from '../../assets/artifacts.svg';
import reviewIcon from '../../assets/review.svg';
import { CloseButton } from '../ui/CloseButton/CloseButton';
import '../subagent/Subagent.css';

export interface PanelTabItem {
  key: string;
  label: string;
  icon?: ReactNode;
  count?: string | number;
  /** Render a close button for closable tabs (desktop browser, plugin panels). */
  closable?: boolean;
  /** Defer closing until the tab's exit animation completes. */
  animateClose?: boolean;
  /** Accessible label for the close button; falls back to the browser tab label. */
  closeLabel?: string;
}

/** The plus button that creates a new blank tab in the tab bar. */
export interface PanelTabAddAction {
  label: string;
  onOpen: () => void;
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

/** Exit animation length; the close callback fires when it ends. */
const TAB_EXIT_MS = 140;

function prefersReducedMotion(): boolean {
  return typeof window !== 'undefined'
    && typeof window.matchMedia === 'function'
    && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
}

export function ExpandedPanelTabs({
  tabs,
  activeTab,
  onTabChange,
  onTabClose,
  onCollapse,
  onToggleFullscreen,
  isFullscreen,
  addTab,
  actions,
  testIdPrefix = 'tool-panel',
  tabListLabel,
  idPrefix,
}: {
  tabs: PanelTabItem[];
  activeTab: string;
  onTabChange: (tab: string) => void;
  onTabClose?: (tab: string) => void;
  onCollapse?: () => void;
  onToggleFullscreen?: () => void;
  isFullscreen?: boolean;
  addTab?: PanelTabAddAction;
  /** Extra controls rendered before the fullscreen and collapse buttons. */
  actions?: ReactNode;
  testIdPrefix?: string;
  tabListLabel?: string;
  idPrefix?: string;
}) {
  const { t } = useTranslation();
  const generatedId = useId();
  const tabPanelId = idPrefix ?? generatedId;
  const tabListRef = useRef<HTMLDivElement | null>(null);
  const closingTimersRef = useRef(new Map<string, number>());
  const onTabCloseRef = useRef(onTabClose);
  /** Measured width per closing tab, so the collapse starts from the real width instead of `auto`. */
  const [closingWidths, setClosingWidths] = useState<ReadonlyMap<string, number>>(() => new Map());

  useEffect(() => {
    onTabCloseRef.current = onTabClose;
  }, [onTabClose]);

  useEffect(() => () => {
    const pending = [...closingTimersRef.current.entries()];
    closingTimersRef.current.clear();
    // Unmounting mid-animation must not swallow the click: finish those closes immediately.
    for (const [key, timer] of pending) {
      window.clearTimeout(timer);
      onTabCloseRef.current?.(key);
    }
  }, []);

  useLayoutEffect(() => {
    // Tabs enter at their final width, so the selected tab needs only one alignment.
    tabListRef.current?.querySelector<HTMLElement>('[role="tab"][aria-selected="true"]')
      ?.scrollIntoView?.({ behavior: 'auto', block: 'nearest', inline: 'nearest' });
  }, [activeTab, tabs.length]);

  const requestTabClose = ({ key, animateClose }: PanelTabItem) => {
    if (!onTabClose || closingTimersRef.current.has(key)) return;
    const width = animateClose && !prefersReducedMotion()
      ? tabListRef.current?.querySelector<HTMLElement>(`[data-tab-key="${key}"]`)?.offsetWidth ?? 0
      : 0;
    if (width === 0) {
      onTabClose(key);
      return;
    }
    setClosingWidths((current) => new Map(current).set(key, width));
    closingTimersRef.current.set(key, window.setTimeout(() => {
      closingTimersRef.current.delete(key);
      setClosingWidths((current) => {
        const next = new Map(current);
        next.delete(key);
        return next;
      });
      onTabCloseRef.current?.(key);
    }, TAB_EXIT_MS));
  };

  return (
    <div data-testid={`${testIdPrefix}-expanded-header`} className="single-agent-tool-tabs min-w-0">
      {/* The strip hugs its tabs and gives the add button its own box beside the scroller, so the
          button can never sit on top of a tab, no matter where the list is scrolled. */}
      <div className="single-agent-tool-tabs__strip">
        <div
          ref={tabListRef}
          data-testid={`${testIdPrefix}-expanded-tabs`}
          className="single-agent-tool-tabs__list"
          role="tablist"
          aria-label={tabListLabel ?? t('team.toolTabs')}
        >
          {tabs.map((tab) => {
            const isActive = activeTab === tab.key;
            const countSuffix = tab.count !== undefined ? ` (${tab.count})` : '';
            const closingWidth = closingWidths.get(tab.key);
            const isClosing = closingWidth !== undefined;
            const closeLabel = tab.closeLabel
              ?? (tab.key === 'browser' ? t('browser.pane.closeTab') : `${t('common.close')} ${tab.label}`);
            return (
              <div
                key={tab.key}
                data-testid={`${testIdPrefix}-tab`}
                data-variant={tab.key}
                data-tab-key={tab.key}
                id={`${tabPanelId}-${encodeURIComponent(tab.key)}`}
                role="tab"
                tabIndex={isClosing ? -1 : 0}
                aria-selected={isActive}
                aria-controls={`${tabPanelId}-panel`}
                title={tab.label}
                className={`single-agent-tool-tab group shrink-0 ${isActive ? 'single-agent-tool-tab--active' : ''}${
                  isClosing ? ' single-agent-tool-tab--closing' : ''
                }`}
                style={isClosing ? ({ '--tab-shrink-from': `${closingWidth}px` } as CSSProperties) : undefined}
                onClick={() => {
                  if (!isClosing) onTabChange(tab.key);
                }}
                onKeyDown={(event) => {
                  // Preserve native close-button activation when keyboard events bubble.
                  if (event.target !== event.currentTarget) return;
                  if (event.key !== 'Enter' && event.key !== ' ') return;
                  event.preventDefault();
                  if (!isClosing) onTabChange(tab.key);
                }}
              >
                {tab.icon}
                <span className="single-agent-tool-tab__label">{tab.label}</span>
                {countSuffix}
                {tab.closable && onTabClose && (
                  <span
                    data-variant={tab.key}
                    className="single-agent-tool-tab__close"
                    onClick={(event) => event.stopPropagation()}
                  >
                    <CloseButton
                      size={16}
                      testId={`${testIdPrefix}-tab-close`}
                      ariaLabel={closeLabel}
                      title={closeLabel}
                      onClick={() => requestTabClose(tab)}
                    />
                  </span>
                )}
              </div>
            );
          })}
        </div>
        {addTab && (
          <button
            type="button"
            onClick={addTab.onOpen}
            data-testid={`${testIdPrefix}-tab-add`}
            className="panel-tab-icon-btn single-agent-tool-tabs__add"
            aria-label={addTab.label}
            title={addTab.label}
          >
            <Plus size={24} aria-hidden="true" />
          </button>
        )}
      </div>

      <div className="single-agent-tool-tabs__actions flex shrink-0 items-center gap-2">
        {actions}
        {onToggleFullscreen && (
          <button
            onClick={onToggleFullscreen}
            data-testid={`${testIdPrefix}-maximize`}
            className="panel-tab-icon-btn"
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
            className="panel-tab-icon-btn"
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
