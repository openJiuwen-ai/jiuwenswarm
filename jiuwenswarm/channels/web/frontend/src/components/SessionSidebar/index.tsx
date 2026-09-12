/**
 * SessionSidebar Component
 *
 * Redesigned sidebar with logo, navigation, and advanced config panel.
 */

import { useState, useRef, useEffect, useLayoutEffect, useCallback } from 'react';
import { useTranslation } from 'react-i18next';
import './SessionSidebar.css';
import ChannelIcon from '../../assets/sidebar/channel.svg?react';
import PluginIcon from '../../assets/sidebar/plugin.svg?react';
import ConfigIcon from '../../assets/sidebar/config.svg?react';
import WebIcon from '../../assets/sidebar/web.svg?react';
import PlusIcon from '../../assets/sidebar/plus.svg?react';
import logoIcon from '/logo.svg';
import AdvancedConfigIcon from '../../assets/sidebar/advanced-config-new.svg?react';
import UpdateIcon from '../../assets/sidebar/advanced-config.svg?react';
import WorkIcon from '../../assets/工作.svg?react';
import SkillDesignIcon from '../../assets/技能.svg?react';
import AgentDesignIcon from '../../assets/智能体.svg?react';
import MoreDesignIcon from '../../assets/更多.svg?react';
import { webRequest } from '../../services/webClient';

type MainNavKey = 'chat' | 'skills' | 'agents' | 'teams' | 'thirdagents' | 'hardware' | 'modelmgmt' | 'users' | 'sessions' | 'cron' | 'channels' | 'extensions' | 'configpanel' | 'browserpanel' | 'updatepanel';

interface SessionSidebarProps {
  activeNav: MainNavKey;
  onNavigate: (nav: MainNavKey) => void;
  appVersion: string;
  isConnected: boolean;
  onNewSession?: () => void;
  showNewSession?: boolean;
  hiddenNavItems?: MainNavKey[];
  onMorePanelOpenChange?: (open: boolean) => void;
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

// 三方 Agent 管理入口图标：机器人（天线 + 方头 + 双眼 + 嘴），内联风格同 teamNavIcon。
const thirdAgentNavIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 7V4.5" />
    <circle cx="12" cy="3.5" r="1" />
    <rect x="4.75" y="7" width="14.5" height="11.5" rx="2.25" />
    <path strokeLinecap="round" strokeWidth={2} d="M9.5 12h.01M14.5 12h.01" />
    <path strokeLinecap="round" d="M10 15.25h4" />
  </svg>
);

// 硬件管理入口图标：芯片（外框 + 内核 + 四向引脚），内联风格同上。
const hardwareNavIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
    <rect x="7" y="7" width="10" height="10" rx="1.5" />
    <rect x="10.25" y="10.25" width="3.5" height="3.5" rx="0.5" />
    <path strokeLinecap="round" d="M9.5 4v3M14.5 4v3M9.5 17v3M14.5 17v3M4 9.5h3M4 14.5h3M17 9.5h3M17 14.5h3" />
  </svg>
);

// 模型管理入口图标：立方体（顶面 + 左右棱线），内联风格同上。
const modelNavIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 3.75l8 4.5v7.5l-8 4.5-8-4.5v-7.5l8-4.5z" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M12 12l8-3.75M12 12v8.25M12 12L4 8.25" />
  </svg>
);

// 用户管理入口图标：人像（头 + 肩），内联风格同上。
const userNavIcon = (
  <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth={1.5}>
    <path strokeLinecap="round" strokeLinejoin="round" d="M15.75 7.5a3.75 3.75 0 11-7.5 0 3.75 3.75 0 017.5 0z" />
    <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 20.25a7.5 7.5 0 0115 0" />
  </svg>
);

const mainNavItems: NavItem[] = [
  { key: 'chat', labelKey: 'nav.work', icon: <WorkIcon aria-hidden /> },
  { key: 'thirdagents', labelKey: 'nav.thirdAgents', icon: thirdAgentNavIcon },
  { key: 'hardware', labelKey: 'nav.hardware', icon: hardwareNavIcon },
  { key: 'modelmgmt', labelKey: 'nav.modelmgmt', icon: modelNavIcon },
  { key: 'users', labelKey: 'nav.users', icon: userNavIcon },
  { key: 'skills', labelKey: 'nav.skills', icon: <SkillDesignIcon aria-hidden /> },
  { key: 'channels', labelKey: 'nav.channels', icon: <ChannelIcon aria-hidden /> },
  { key: 'agents', labelKey: 'nav.agent', icon: <AgentDesignIcon aria-hidden /> },
  { key: 'teams', labelKey: 'nav.teams', icon: teamNavIcon },
];

const moreNavItems: NavItem[] = [
  { key: 'configpanel', labelKey: 'nav.config', icon: <ConfigIcon aria-hidden /> },
  { key: 'extensions', labelKey: 'nav.extensions', icon: <PluginIcon aria-hidden /> },
  { key: 'browserpanel', labelKey: 'nav.browser', icon: <WebIcon aria-hidden /> },
  { key: 'updatepanel', labelKey: 'nav.update', icon: <UpdateIcon aria-hidden /> },
];

// Advanced Config Panel Component
function AdvancedConfigPanel({
  isOpen,
  onClose,
  appVersion,
  isConnected,
  buttonRef,
}: {
  isOpen: boolean;
  onClose: () => void;
  appVersion: string;
  isConnected: boolean;
  buttonRef: React.RefObject<HTMLButtonElement>;
}) {
  const { i18n, t } = useTranslation();
  const panelRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    function handleClickOutside(event: MouseEvent) {
      if (
        panelRef.current &&
        !panelRef.current.contains(event.target as Node) &&
        buttonRef.current &&
        !buttonRef.current.contains(event.target as Node)
      ) {
        onClose();
      }
    }
    if (isOpen) {
      document.addEventListener('mousedown', handleClickOutside);
    }
    return () => {
      document.removeEventListener('mousedown', handleClickOutside);
    };
  }, [isOpen, onClose, buttonRef]);

  const handleLanguageChange = (lang: 'zh' | 'en') => {
    i18n.changeLanguage(lang);
    void webRequest('locale.set_conf', { preferred_language: lang }).catch(() => {});
  };

  const isZh = i18n.language.startsWith('zh');

  if (!isOpen) return null;

  return (
    <div ref={panelRef} className="advanced-config-panel" data-testid="session-sidebar-advanced-config-panel">
      <div className="config-row" data-testid="session-sidebar-config-row-connection">
        <span className="config-row__label" data-testid="session-sidebar-config-row-connection-label">{t('sessionSidebar.connectionStatus')}</span>
        <div className={`connection-status ${isConnected ? 'connection-status--connected' : 'connection-status--disconnected'}`} data-testid="session-sidebar-connection-status" data-variant={isConnected ? 'connected' : 'disconnected'}>
          <span className="connection-status__dot" />
          <span className="connection-status__text">
            {isConnected ? t('connection.connected') : t('connection.disconnected')}
          </span>
        </div>
      </div>

      {appVersion && (
        <div className="config-row" data-testid="session-sidebar-config-row-version">
          <span className="config-row__label">{t('sessionSidebar.version')}</span>
          <span className="config-row__value">{appVersion}</span>
        </div>
      )}

      <div className="config-row" data-testid="session-sidebar-config-row-language">
        <span className="config-row__label">{t('sessionSidebar.language')}</span>
        <div className="segmented-control" data-testid="session-sidebar-language-segmented">
          <button
            className={`segmented-control__btn ${isZh ? 'segmented-control__btn--active' : ''}`}
            onClick={() => handleLanguageChange('zh')}
            aria-pressed={isZh}
            data-testid="session-sidebar-language-zh"
          >
            中
          </button>
          <button
            className={`segmented-control__btn ${!isZh ? 'segmented-control__btn--active' : ''}`}
            onClick={() => handleLanguageChange('en')}
            aria-pressed={!isZh}
            data-testid="session-sidebar-language-en"
          >
            En
          </button>
        </div>
      </div>

    </div>
  );
}

export function SessionSidebar({
  activeNav,
  onNavigate,
  appVersion,
  isConnected,
  onNewSession,
  showNewSession = true,
  hiddenNavItems = [],
  onMorePanelOpenChange,
}: SessionSidebarProps) {
  const { t } = useTranslation();
  const [advancedConfigOpen, setAdvancedConfigOpen] = useState(false);
  const settingsRef = useRef<HTMLButtonElement>(null);

  const handleNewSession = useCallback(() => {
    onNavigate('chat');
    if (onNewSession) {
      onNewSession();
    }
  }, [onNavigate, onNewSession]);

  const toggleAdvancedConfig = () => {
    setAdvancedConfigOpen(!advancedConfigOpen);
  };

  const handleMoreClick = () => {
    if (!isMoreActive) {
      const defaultMoreNav = visibleMoreNavItems[0]?.key;
      if (defaultMoreNav) {
        onNavigate(defaultMoreNav);
      }
    }
  };

  const handleNavClick = (nav: MainNavKey) => {
    onNavigate(nav);
  };

  const handleMoreNavClick = (nav: MainNavKey) => {
    onNavigate(nav);
  };

  const getNavItemLabel = (item: NavItem) => t(item.labelKey);
  const visibleMainNavItems = mainNavItems.filter((item) => !hiddenNavItems.includes(item.key));
  const visibleMoreNavItems = moreNavItems.filter((item) => !hiddenNavItems.includes(item.key));
  const isMoreActive = visibleMoreNavItems.some((item) => item.key === activeNav);
  // 定时任务（cron）是"任务"区内与会话同级的视图，没有独立的导航图标，
  // 因此进入定时任务时"任务"导航项也应保持选中态
  const isNavItemActive = (item: NavItem) =>
    activeNav === item.key || (item.key === 'chat' && activeNav === 'cron');

  useLayoutEffect(() => {
    onMorePanelOpenChange?.(isMoreActive);
    return () => onMorePanelOpenChange?.(false);
  }, [isMoreActive, onMorePanelOpenChange]);

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
        >
          <span className="icon-rail-nav-item__icon">{item.icon}</span>
          <span className="icon-rail-nav-item__label">{getNavItemLabel(item)}</span>
        </button>
      ))}

      {visibleMoreNavItems.length > 0 && (
        <>
          <button
            className={`icon-rail-nav-item${isMoreActive ? ' icon-rail-nav-item--active' : ''}`}
            onClick={handleMoreClick}
            aria-expanded={isMoreActive}
            data-model-setup-guide-target="more"
            data-testid="session-sidebar-more-trigger"
          >
            <span className="icon-rail-nav-item__icon">
              <MoreDesignIcon aria-hidden />
            </span>
            <span className="icon-rail-nav-item__label">{t('nav.more')}</span>
          </button>
          {isMoreActive && (
            <div className="icon-rail-more-panel" data-testid="session-sidebar-more-panel">
              <div className="icon-rail-more-panel__title" data-testid="session-sidebar-more-panel-title">{t('sessionSidebar.moreSettings')}</div>
              <nav className="icon-rail-more-panel__list" aria-label={t('sessionSidebar.moreSettings')} data-testid="session-sidebar-more-nav">
                {visibleMoreNavItems.map((item) => (
                  <button
                    key={item.key}
                    className={`icon-rail-more-panel__item${activeNav === item.key ? ' icon-rail-more-panel__item--active' : ''}`}
                    onClick={() => handleMoreNavClick(item.key)}
                    data-testid="session-sidebar-more-nav-item"
                    data-variant={item.key}
                  >
                    <span className="icon-rail-more-panel__icon">{item.icon}</span>
                    <span className="icon-rail-more-panel__text">{getNavItemLabel(item)}</span>
                  </button>
                ))}
              </nav>
            </div>
          )}
        </>
      )}

      <div className="icon-rail-spacer" />

      <button
        ref={settingsRef}
        className="icon-rail-nav-item icon-rail-settings-button"
        onClick={toggleAdvancedConfig}
        aria-label={t('sessionSidebar.moreSettings')}
        data-testid="session-sidebar-advanced-config-trigger"
      >
        <span className="icon-rail-nav-item__icon">
          <AdvancedConfigIcon aria-hidden width="16" height="16" />
        </span>
      </button>

      <AdvancedConfigPanel
        isOpen={advancedConfigOpen}
        onClose={() => setAdvancedConfigOpen(false)}
        appVersion={appVersion}
        isConnected={isConnected}
        buttonRef={settingsRef}
      />
    </aside>
  );
}
