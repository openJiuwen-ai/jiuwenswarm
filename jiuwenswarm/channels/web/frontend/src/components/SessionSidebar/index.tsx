/**
 * SessionSidebar Component
 *
 * Redesigned sidebar with logo, navigation, and advanced config panel.
 */

import { useState, useRef, useEffect, useLayoutEffect, useCallback, useMemo } from 'react';
import { useTranslation } from 'react-i18next';
import './SessionSidebar.css';
import ChannelIcon from '../../assets/sidebar/channel.svg?react';
import PluginIcon from '../../assets/sidebar/plugin.svg?react';
import ConfigIcon from '../../assets/sidebar/config.svg?react';
import A2AIcon from '../../assets/sidebar/a2a.svg?react';
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
import {
  agentContextKey,
  formatAgentContextLabel,
  useEnterpriseContext,
} from '../../services/enterpriseContext';
import { isClickOutside } from './clickOutside';
import { EditableCombobox } from './EditableCombobox';
import type { MainNavKey } from '../../features/mainNavigationState';

type ContextMode = 'select' | 'custom';

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
    <path
      strokeLinecap="round"
      strokeLinejoin="round"
      d="M18 18.72a8.96 8.96 0 01-12 0m12 0a3.75 3.75 0 00-6 0m6 0A8.96 8.96 0 0012 15.75a8.96 8.96 0 00-6 2.97m12 0A9 9 0 1012 21a8.96 8.96 0 006-2.28zM15 9.75a3 3 0 11-6 0 3 3 0 016 0zm6 3a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0zm-13.5 0a2.25 2.25 0 11-4.5 0 2.25 2.25 0 014.5 0z"
    />
  </svg>
);

const mainNavItems: NavItem[] = [
  { key: 'chat', labelKey: 'nav.work', icon: <WorkIcon aria-hidden /> },
  { key: 'skills', labelKey: 'nav.skills', icon: <SkillDesignIcon aria-hidden /> },
  { key: 'channels', labelKey: 'nav.channels', icon: <ChannelIcon aria-hidden /> },
  { key: 'agents', labelKey: 'nav.agent', icon: <AgentDesignIcon aria-hidden /> },
  { key: 'teams', labelKey: 'nav.teams', icon: teamNavIcon },
];

const moreNavItems: NavItem[] = [
  { key: 'configpanel', labelKey: 'nav.config', icon: <ConfigIcon aria-hidden /> },
  { key: 'extensions', labelKey: 'nav.extensions', icon: <PluginIcon aria-hidden /> },
  { key: 'browserpanel', labelKey: 'nav.browser', icon: <WebIcon aria-hidden /> },
  { key: 'a2aingress', labelKey: 'nav.a2aIngress', icon: <A2AIcon aria-hidden /> },
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
      if (panelRef.current && !panelRef.current.contains(event.target as Node) && buttonRef.current && !buttonRef.current.contains(event.target as Node)) {
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
    <div ref={panelRef} className="advanced-config-panel">
      <div className="config-row">
        <span className="config-row__label">{t('sessionSidebar.connectionStatus')}</span>
        <div className={`connection-status ${isConnected ? 'connection-status--connected' : 'connection-status--disconnected'}`}>
          <span className="connection-status__dot" />
          <span className="connection-status__text">{isConnected ? t('connection.connected') : t('connection.disconnected')}</span>
        </div>
      </div>

      {appVersion && (
        <div className="config-row">
          <span className="config-row__label">{t('sessionSidebar.version')}</span>
          <span className="config-row__value">{appVersion}</span>
        </div>
      )}

      <div className="config-row">
        <span className="config-row__label">{t('sessionSidebar.language')}</span>
        <div className="segmented-control">
          <button className={`segmented-control__btn ${isZh ? 'segmented-control__btn--active' : ''}`} onClick={() => handleLanguageChange('zh')}>
            中
          </button>
          <button className={`segmented-control__btn ${!isZh ? 'segmented-control__btn--active' : ''}`} onClick={() => handleLanguageChange('en')}>
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
  const enterprise = useEnterpriseContext();
  const [contextOpen, setContextOpen] = useState(false);
  const [contextMode, setContextMode] = useState<ContextMode>('select');
  const [listSelectionKey, setListSelectionKey] = useState('');
  const [customBotId, setCustomBotId] = useState('');
  const [customGroupId, setCustomGroupId] = useState('');
  const [customUserId, setCustomUserId] = useState('');
  const contextButtonRef = useRef<HTMLButtonElement>(null);
  const contextPanelRef = useRef<HTMLDivElement>(null);

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
  const visibleMainNavItems = mainNavItems.filter(item => !hiddenNavItems.includes(item.key));
  const visibleMoreNavItems = moreNavItems.filter(item => !hiddenNavItems.includes(item.key));
  const isMoreActive = visibleMoreNavItems.some(item => item.key === activeNav);
  // 定时任务（cron）是"工作"区内与会话同级的视图，没有独立的导航图标，
  // 因此进入定时任务时"工作"导航项也应保持选中态
  const isNavItemActive = (item: NavItem) => activeNav === item.key || (item.key === 'chat' && activeNav === 'cron');

  useLayoutEffect(() => {
    onMorePanelOpenChange?.(isMoreActive);
    return () => onMorePanelOpenChange?.(false);
  }, [isMoreActive, onMorePanelOpenChange]);

  useEffect(() => {
    if (!contextOpen) return;

    const handleClickOutside = (event: MouseEvent) => {
      if (isClickOutside(event.target as Node | null, [contextPanelRef.current, contextButtonRef.current])) {
        setContextOpen(false);
      }
    };

    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [contextOpen]);

  useEffect(() => {
    if (!enterprise || !contextOpen) return;
    setCustomBotId(enterprise.selected.bot_id);
    setCustomGroupId(enterprise.selected.group_id);
    setCustomUserId(enterprise.selected.user_id);
  }, [contextOpen, enterprise?.selected]);

  const activeMode: ContextMode = useMemo(() => {
    if (!enterprise) return 'select';
    const selectedKey = agentContextKey(enterprise.selected);
    return enterprise.contexts.some(item => agentContextKey(item) === selectedKey) ? 'select' : 'custom';
  }, [enterprise]);

  // 生效身份变化后（含自定义应用后刷新）自动落到对应页签，避免误以为还在「选择 Agent」。
  useEffect(() => {
    setContextMode(activeMode);
  }, [activeMode]);

  // 「选择 Agent」下拉只跟授权列表走；自定义生效时不预选第一项，
  // 否则点选同一项时 combobox 认为值未变、不会触发切换。
  useEffect(() => {
    if (!enterprise?.contexts.length) return;
    const selectedKey = agentContextKey(enterprise.selected);
    if (enterprise.contexts.some(item => agentContextKey(item) === selectedKey)) {
      setListSelectionKey(selectedKey);
      return;
    }
    setListSelectionKey('');
  }, [
    enterprise?.contexts,
    enterprise?.selected.bot_id,
    enterprise?.selected.group_id,
    enterprise?.selected.user_id,
  ]);

  const agentContextOptions = useMemo(
    () =>
      (enterprise?.contexts ?? []).map(item => ({
        value: agentContextKey(item),
        label: formatAgentContextLabel(item),
      })),
    [enterprise?.contexts],
  );
  return (
    <aside className="sidebar sidebar--icon-rail">
      <div className="icon-rail-logo">
        <img src={logoIcon} alt="Logo" width="28" height="28" />
      </div>

      {showNewSession && (
        <button className="icon-rail-nav-item" onClick={handleNewSession}>
          <span className="icon-rail-nav-item__icon">
            <PlusIcon aria-hidden width="16" height="16" />
          </span>
          <span className="icon-rail-nav-item__label">{t('chat.newSession')}</span>
        </button>
      )}

      {visibleMainNavItems.map(item => (
        <button
          key={item.key}
          className={`icon-rail-nav-item${isNavItemActive(item) ? ' icon-rail-nav-item--active' : ''}`}
          onClick={() => handleNavClick(item.key)}
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
          >
            <span className="icon-rail-nav-item__icon">
              <MoreDesignIcon aria-hidden />
            </span>
            <span className="icon-rail-nav-item__label">{t('nav.more')}</span>
          </button>
          {isMoreActive && (
            <div className="icon-rail-more-panel">
              <div className="icon-rail-more-panel__title">{t('sessionSidebar.moreSettings')}</div>
              <nav className="icon-rail-more-panel__list" aria-label={t('sessionSidebar.moreSettings')}>
                {visibleMoreNavItems.map(item => (
                  <button
                    key={item.key}
                    className={`icon-rail-more-panel__item${activeNav === item.key ? ' icon-rail-more-panel__item--active' : ''}`}
                    onClick={() => handleMoreNavClick(item.key)}
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

      {enterprise && (
        <button
          ref={contextButtonRef}
          type="button"
          className={`icon-rail-nav-item${contextOpen ? ' icon-rail-nav-item--active' : ''}`}
          onClick={() => setContextOpen(open => !open)}
          aria-label={t('sessionSidebar.enterpriseContext.userContext')}
          title={t('sessionSidebar.enterpriseContext.userContext')}
        >
          <span className="icon-rail-nav-item__icon">{(enterprise.user.display_name || enterprise.user.user_id).slice(0, 1).toUpperCase()}</span>
          <span className="icon-rail-nav-item__label">{t('sessionSidebar.enterpriseContext.user')}</span>
        </button>
      )}
      {enterprise && contextOpen && (
        <div ref={contextPanelRef} className="enterprise-context-popover">
          <div className="enterprise-context-popover__user">
            {enterprise.user.display_name || enterprise.user.user_id}
            <small>{enterprise.user.user_id}</small>
          </div>
          <div className="enterprise-context-popover__modes" role="tablist" aria-label={t('sessionSidebar.enterpriseContext.modeAria')}>
            <button
              type="button"
              role="tab"
              aria-selected={contextMode === 'select'}
              className={`enterprise-context-popover__mode${contextMode === 'select' ? ' enterprise-context-popover__mode--active' : ''}`}
              disabled={enterprise.contextSwitching}
              onClick={() => {
                // 自定义生效时清空下拉，避免仍显示上次项导致再点同一项无反应。
                if (activeMode === 'custom') setListSelectionKey('');
                setContextMode('select');
              }}
            >
              {t('sessionSidebar.enterpriseContext.selectAgent')}
              {activeMode === 'select' && (
                <span className="enterprise-context-popover__mode-check" aria-hidden>
                  ✓
                </span>
              )}
            </button>
            <button
              type="button"
              role="tab"
              aria-selected={contextMode === 'custom'}
              className={`enterprise-context-popover__mode${contextMode === 'custom' ? ' enterprise-context-popover__mode--active' : ''}`}
              disabled={enterprise.contextSwitching}
              onClick={() => setContextMode('custom')}
            >
              {t('sessionSidebar.enterpriseContext.custom')}
              {activeMode === 'custom' && (
                <span className="enterprise-context-popover__mode-check" aria-hidden>
                  ✓
                </span>
              )}
            </button>
          </div>
          {contextMode === 'select' ? (
            <label>
              {t('sessionSidebar.enterpriseContext.agentOrgLabel')}
              <EditableCombobox
                ariaLabel={t('sessionSidebar.enterpriseContext.agentOrgLabel')}
                emptyText={t('sessionSidebar.enterpriseContext.noMatch')}
                placeholder={t('sessionSidebar.enterpriseContext.pleaseSelect')}
                value={listSelectionKey}
                disabled={enterprise.contextSwitching}
                options={agentContextOptions}
                onChange={key => {
                  setListSelectionKey(key);
                  enterprise.onContextChange(key);
                }}
              />
            </label>
          ) : (
            <>
              <label>
                {t('sessionSidebar.enterpriseContext.botId')}
                <input
                  className="enterprise-context-popover__input"
                  value={customBotId}
                  disabled={enterprise.contextSwitching}
                  onChange={event => setCustomBotId(event.target.value)}
                  aria-label={t('sessionSidebar.enterpriseContext.botId')}
                />
              </label>
              <label>
                {t('sessionSidebar.enterpriseContext.groupId')}
                <input
                  className="enterprise-context-popover__input"
                  value={customGroupId}
                  disabled={enterprise.contextSwitching}
                  onChange={event => setCustomGroupId(event.target.value)}
                  aria-label={t('sessionSidebar.enterpriseContext.groupId')}
                />
              </label>
              <label>
                {t('sessionSidebar.enterpriseContext.userId')}
                <input
                  className="enterprise-context-popover__input"
                  value={customUserId}
                  disabled={enterprise.contextSwitching}
                  onChange={event => setCustomUserId(event.target.value)}
                  aria-label={t('sessionSidebar.enterpriseContext.userId')}
                />
              </label>
              <button
                type="button"
                className="enterprise-context-popover__apply"
                disabled={enterprise.contextSwitching}
                onClick={() =>
                  enterprise.onCustomContextApply({
                    botId: customBotId,
                    groupId: customGroupId,
                    userId: customUserId,
                  })
                }
              >
                {t('sessionSidebar.enterpriseContext.apply')}
              </button>
            </>
          )}
          {enterprise.contextSwitching && (
            <div className="enterprise-context-popover__status">{t('sessionSidebar.enterpriseContext.switching')}</div>
          )}
          {enterprise.contextError && <div className="enterprise-context-popover__error">{enterprise.contextError}</div>}
          <button type="button" className="enterprise-context-popover__logout" onClick={enterprise.onLogout}>
            {t('sessionSidebar.enterpriseContext.logout')}
          </button>
        </div>
      )}

      <button ref={settingsRef} className="icon-rail-nav-item" onClick={toggleAdvancedConfig} aria-label={t('sessionSidebar.moreSettings')}>
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
