/**
 * SessionSidebar Component
 *
 * Redesigned sidebar with logo and navigation.
 */

import { useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import './SessionSidebar.css';
import PlusIcon from '../../assets/sidebar/plus.svg?react';
import logoIcon from '/logo.svg';
import SettingsIcon from '../../assets/settings/app-navigation/settings.svg?react';
import UpdateIcon from '../../assets/sidebar/advanced-config.svg?react';
import WorkIcon from '../../assets/工作.svg?react';
import SkillDesignIcon from '../../assets/agent-management/agent-skill.svg?react';
import AgentDesignIcon from '../../assets/智能体.svg?react';
import type { SidebarNavKey } from '../../utils/frontendPlatform';

type MainNavKey = SidebarNavKey | 'connectorMarket';

interface SessionSidebarProps {
  activeNav: MainNavKey;
  onNavigate: (nav: MainNavKey) => void;
  onNewSession?: () => void;
  showNewSession?: boolean;
  hiddenNavItems?: MainNavKey[];
}

interface NavItem {
  key: MainNavKey;
  labelKey: string;
  icon: React.ReactNode;
}

// "扩展"（连接器市场：插件+MCP）导航图标——和 plugin.svg（Harness 插件管理，纯命名撞车、
// 业务无关）故意区分开，用网格/市场的视觉隐喻而不是拼图块。
const connectorMarketNavIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
    <rect x="3" y="3" width="7" height="7" rx="1.5" strokeLinecap="round" strokeLinejoin="round" />
    <rect x="14" y="3" width="7" height="7" rx="1.5" strokeLinecap="round" strokeLinejoin="round" />
    <rect x="3" y="14" width="7" height="7" rx="1.5" strokeLinecap="round" strokeLinejoin="round" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M17.5 14v7m-3.5-3.5h7" />
  </svg>
);

const experimentsNavIcon = (
  <svg viewBox="0 0 16 16" xmlns="http://www.w3.org/2000/svg" width="16.000000" height="16.000000" fill="none">
    <rect id="rsi" width="16.000000" height="16.000000" x="0.000000" y="0.000000" />
    <path id="形状结合" d="M10.25 1.5C10.42 1.5 10.58 1.53 10.73 1.59C10.88 1.65 11.01 1.74 11.13 1.86C11.25 1.98 11.34 2.12 11.4 2.26C11.46 2.41 11.49 2.57 11.49 2.74C11.49 2.91 11.46 3.07 11.4 3.22C11.34 3.37 11.25 3.5 11.13 3.62C11.01 3.74 10.87 3.83 10.73 3.89C10.66 3.92 10.58 3.94 10.5 3.96L10.5 6.45C10.5 6.45 10.5 6.54 10.52 6.59C10.52 6.59 10.55 6.68 10.58 6.72L13.51 11.44C13.72 11.77 13.82 12.11 13.83 12.45C13.83 12.79 13.75 13.13 13.56 13.47C13.37 13.81 13.13 14.07 12.84 14.24C12.55 14.41 12.21 14.5 11.82 14.5L4.21 14.5C3.82 14.5 3.48 14.41 3.19 14.24C2.9 14.07 2.66 13.81 2.47 13.47C2.28 13.13 2.19 12.79 2.2 12.45C2.2 12.11 2.32 11.78 2.52 11.44L5.45 6.72C5.45 6.72 5.49 6.64 5.51 6.59C5.52 6.55 5.53 6.5 5.53 6.45L5.53 3.96C5.45 3.94 5.37 3.92 5.3 3.89C5.15 3.83 5.02 3.74 4.9 3.62C4.78 3.5 4.69 3.36 4.63 3.22C4.57 3.07 4.54 2.91 4.54 2.74C4.54 2.57 4.57 2.41 4.63 2.26C4.69 2.11 4.78 1.98 4.9 1.86C5.02 1.74 5.16 1.65 5.3 1.59C5.45 1.53 5.61 1.5 5.78 1.5L10.28 1.5L10.25 1.5ZM10.44 2.94C10.44 2.94 10.34 3 10.25 3L5.75 3C5.67 3 5.6 2.98 5.56 2.94C5.52 2.9 5.5 2.84 5.5 2.75C5.5 2.66 5.52 2.6 5.56 2.56C5.6 2.52 5.66 2.5 5.75 2.5L10.25 2.5C10.33 2.5 10.4 2.52 10.44 2.56C10.48 2.6 10.5 2.66 10.5 2.75C10.5 2.84 10.48 2.9 10.44 2.94ZM6.5 4L9.5 4L9.5 6.46C9.5 6.6 9.52 6.74 9.56 6.87C9.6 7 9.65 7.12 9.72 7.23C9.49 7.2 9.27 7.2 9.07 7.21C8.74 7.23 8.33 7.34 7.84 7.52C7.43 7.67 7.11 7.75 6.88 7.77C6.62 7.79 6.33 7.76 6.02 7.68L6.29 7.24C6.37 7.12 6.42 6.99 6.46 6.86C6.5 6.73 6.52 6.59 6.52 6.45L6.52 3.99L6.5 4ZM9.13 8.22C9.51 8.19 9.95 8.27 10.46 8.45L12.65 11.98C12.75 12.15 12.81 12.31 12.81 12.48C12.81 12.65 12.77 12.82 12.67 12.99C12.57 13.16 12.45 13.29 12.31 13.38C12.16 13.47 11.99 13.51 11.8 13.51L4.19 13.51C3.99 13.51 3.82 13.47 3.68 13.38C3.53 13.29 3.41 13.17 3.32 12.99C3.22 12.82 3.18 12.65 3.18 12.48C3.18 12.31 3.24 12.14 3.34 11.98L5.45 8.59C5.99 8.76 6.49 8.82 6.94 8.79C7.27 8.77 7.68 8.66 8.17 8.48C8.58 8.33 8.9 8.25 9.13 8.23L9.13 8.22Z" fill="rgb(25,25,25)" fill-rule="evenodd" />
  </svg>
);

const mainNavItems: NavItem[] = [
  { key: 'chat', labelKey: 'nav.work', icon: <WorkIcon aria-hidden /> },
  { key: 'agents', labelKey: 'nav.agent', icon: <AgentDesignIcon aria-hidden /> },
  { key: 'skills', labelKey: 'nav.skills', icon: <SkillDesignIcon aria-hidden /> },
  { key: 'connectorMarket', labelKey: 'nav.connectorMarket', icon: connectorMarketNavIcon },
  { key: 'experiments', labelKey: 'nav.experiments', icon: experimentsNavIcon },
  { key: 'settings', labelKey: 'nav.settings', icon: <SettingsIcon aria-hidden /> },
  { key: 'updatepanel', labelKey: 'nav.update', icon: <UpdateIcon aria-hidden /> },
];

export function SessionSidebar({
  activeNav,
  onNavigate,
  onNewSession,
  showNewSession = true,
  hiddenNavItems = [],
}: SessionSidebarProps) {
  const { t } = useTranslation();

  const handleNewSession = useCallback(() => {
    onNavigate('chat');
    if (onNewSession) {
      onNewSession();
    }
  }, [onNavigate, onNewSession]);

  const handleNavClick = (nav: MainNavKey) => {
    onNavigate(nav);
  };

  const getNavItemLabel = (item: NavItem) => t(item.labelKey);
  const visibleMainNavItems = mainNavItems.filter((item) => !hiddenNavItems.includes(item.key));
  // 定时任务（cron）是"任务"区内与会话同级的视图，没有独立的导航图标，
  // 因此进入定时任务时"任务"导航项也应保持选中态
  const isNavItemActive = (item: NavItem) =>
    activeNav === item.key || (item.key === 'chat' && activeNav === 'cron');

  return (
    <aside className="sidebar sidebar--icon-rail" data-testid="session-sidebar-rail">
      <div className="icon-rail-logo" data-testid="session-sidebar-logo">
        <img src={logoIcon} alt="Logo" width="28" height="28" />
      </div>

      {showNewSession && (
        <button
          className="icon-rail-nav-item"
          onClick={handleNewSession}
          data-testid="session-sidebar-new-session-button"
        >
          <span className="icon-rail-nav-item__icon">
            <PlusIcon aria-hidden width="16" height="16" />
          </span>
          <span className="icon-rail-nav-item__label">{t('chat.newSession')}</span>
        </button>
      )}

      {visibleMainNavItems.map((item) => (
        <button
          key={item.key}
          className={`icon-rail-nav-item${isNavItemActive(item) ? ' icon-rail-nav-item--active' : ''}`}
          onClick={() => handleNavClick(item.key)}
          data-testid="session-sidebar-nav-item"
          data-variant={item.key}
          data-model-setup-guide-target={item.key === 'settings' ? 'settings' : undefined}
        >
          <span className="icon-rail-nav-item__icon">{item.icon}</span>
          <span className="icon-rail-nav-item__label">{getNavItemLabel(item)}</span>
        </button>
      ))}

      <div className="icon-rail-spacer" />
    </aside>
  );
}
