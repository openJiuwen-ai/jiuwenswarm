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

const teamNavIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M18 18.72a8.96 8.96 0 01-12 0m12 0a3.75 3.75 0 00-6 0m6 0A8.96 8.96 0 0012 15.75a8.96 8.96 0 00-6 2.97m12 0A9 9 0 1012 21a8.96 8.96 0 006-2.28zM15 9.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z" />
  </svg>
);

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

// "个人上下文"导航图标——文档/知识库隐喻，内联 SVG 避免引入缺失的图标资源。
const personalContextNavIcon = (
  <svg viewBox="0 0 16 16" width="16" height="16" fill="none">
    <path
      fillRule="evenodd"
      d="M12 3C13.1046 3 14 3.89543 14 5C14 6.10457 13.1046 7 12 7L2 7C2 7 2.99999 6 2.99999 5C2.99999 4 2 3 2 3L12 3Z"
      stroke="currentColor"
      strokeLinejoin="round"
      strokeWidth={1}
    />
    <path
      fillRule="evenodd"
      d="M10 0C11.1046 0 12 0.89543 12 2C12 3.10457 11.1046 4 10 4L0 4C0 4 0.999991 3 0.999991 2C0.999992 1 0 0 0 0L10 0Z"
      stroke="currentColor"
      strokeLinejoin="round"
      strokeWidth={1}
      transform="matrix(-1,0,0,1,14,9)"
    />
  </svg>
);

const mainNavItems: NavItem[] = [
  { key: 'chat', labelKey: 'nav.work', icon: <WorkIcon aria-hidden /> },
  { key: 'skills', labelKey: 'nav.skills', icon: <SkillDesignIcon aria-hidden /> },
  { key: 'personalContext', labelKey: 'nav.personalContext', icon: personalContextNavIcon },
  { key: 'agents', labelKey: 'nav.agent', icon: <AgentDesignIcon aria-hidden /> },
  { key: 'connectorMarket', labelKey: 'nav.connectorMarket', icon: connectorMarketNavIcon },
  { key: 'teams', labelKey: 'nav.teams', icon: teamNavIcon },
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
