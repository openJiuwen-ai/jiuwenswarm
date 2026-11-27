/**
 * SessionSidebar 组件
 *
 * 会话侧边栏，显示会话列表
 */

import { useTranslation } from 'react-i18next';
import { FEATURE_APP_UPDATER_UI } from '../../featureFlags';
import { OffloadFilesWidget } from './OffloadFilesWidget';
import './SessionSidebar.css';

type MainNavKey = 'chat' | 'skills' | 'agents' | 'sessions' | 'heartbeat' | 'cron' | 'channels' | 'extensions' | 'configpanel' | 'logspanel' | 'browserpanel' | 'updatepanel' | 'skilldev';

interface SessionSidebarProps {
  activeNav: MainNavKey;
  onNavigate: (nav: MainNavKey) => void;
  sessionId: string;
  appVersion: string;
}

export function SessionSidebar({
  activeNav,
  onNavigate,
  sessionId,
  appVersion,
}: SessionSidebarProps) {
  const { t } = useTranslation();
  return (
    <aside className="nav flex flex-col">
      <div className="session-sidebar-group-title session-sidebar-group-title--uppercase">
        {t('nav.chat')}
      </div>
      <div className="space-y-1 mb-4">
        <button
          onClick={() => onNavigate('chat')}
          className={`nav-item w-full ${activeNav === 'chat' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M8 12h.01M12 12h.01M16 12h.01M21 12c0 4.418-4.03 8-9 8a9.863 9.863 0 01-4.255-.949L3 20l1.395-3.72C3.512 15.042 3 13.574 3 12c0-4.418 4.03-8 9-8s9 3.582 9 8z" />
          </svg>
          {t('nav.chat')}
        </button>
        <button
          onClick={() => onNavigate('skilldev')}
          className={`nav-item w-full ${activeNav === 'skilldev' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M17.25 6.75L22.5 12l-5.25 5.25m-10.5 0L1.5 12l5.25-5.25m7.5-3l-4.5 16.5" />
          </svg>
          {t('nav.skilldev', 'Skill Dev')}
        </button>
      </div>

      <div className="session-sidebar-group-title">
        {t('nav.agent')}
      </div>
      <div className="space-y-1">
        <button
          onClick={() => onNavigate('agents')}
          className={`nav-item w-full ${activeNav === 'agents' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.75 17L9 20l-1 1h8l-1-1-.75-3M3 13h18M5 17h14a2 2 0 002-2V5a2 2 0 00-2-2H5a2 2 0 00-2 2v10a2 2 0 002 2z" />
          </svg>
          {t('nav.agent')}
        </button>
        <button
          onClick={() => onNavigate('sessions')}
          className={`nav-item w-full ${activeNav === 'sessions' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m6-6H6m9-9h3a2.25 2.25 0 012.25 2.25v3M9 3H6a2.25 2.25 0 00-2.25 2.25v3m0 6v3A2.25 2.25 0 006 19.75h3m6 0h3a2.25 2.25 0 002.25-2.25v-3" />
          </svg>
          {t('nav.sessions')}
        </button>
        <button
          onClick={() => onNavigate('heartbeat')}
          className={`nav-item w-full ${activeNav === 'heartbeat' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 12h3.75l1.5-4.5 3 9 2.25-6h6" />
          </svg>
          {t('nav.heartbeat')}
        </button>
        <button
          onClick={() => onNavigate('cron')}
          className={`nav-item w-full ${activeNav === 'cron' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 8v4l3 3m6-3a9 9 0 11-18 0 9 9 0 0118 0z" />
          </svg>
          {t('nav.cron')}
        </button>
        <button
          onClick={() => onNavigate('skills')}
          className={`nav-item w-full ${activeNav === 'skills' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 13.5l10.5-11.25L12 10.5h8.25L9.75 21.75 12 13.5H3.75z" />
          </svg>
          {t('nav.skills')}
        </button>
        <button
          onClick={() => onNavigate('channels')}
          className={`nav-item w-full ${activeNav === 'channels' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M4.5 6.75h15m-15 5.25h15m-15 5.25h15" />
          </svg>
          {t('nav.channels')}
        </button>
        <button
          onClick={() => onNavigate('extensions')}
          className={`nav-item w-full ${activeNav === 'extensions' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M12 6v12m6-6H6" />
            <circle cx="12" cy="12" r="9" />
          </svg>
          {t('nav.extensions', 'Extensions')}
        </button>
      </div>

      <div className="session-sidebar-group-title session-sidebar-group-title--with-top-gap">
        {t('nav.settings')}
      </div>
      <div className="space-y-1">
        <button
          onClick={() => onNavigate('configpanel')}
          className={`nav-item w-full ${activeNav === 'configpanel' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M9.594 3.94c.09-.542.56-.94 1.11-.94h2.593c.55 0 1.02.398 1.11.94l.213 1.281c.063.374.313.686.645.87.074.04.147.083.22.127.325.196.72.257 1.075.124l1.217-.456a1.125 1.125 0 011.37.49l1.296 2.247a1.125 1.125 0 01-.26 1.431l-1.003.827c-.293.241-.438.613-.43.992a7.723 7.723 0 010 .255c-.008.378.137.75.43.991l1.004.827c.424.35.534.955.26 1.43l-1.298 2.247a1.125 1.125 0 01-1.369.491l-1.217-.456c-.355-.133-.75-.072-1.076.124a6.47 6.47 0 01-.22.128c-.331.183-.581.495-.644.869l-.213 1.281c-.09.543-.56.941-1.11.941h-2.594c-.55 0-1.019-.398-1.11-.94l-.213-1.281c-.062-.374-.312-.686-.644-.87a6.52 6.52 0 01-.22-.127c-.325-.196-.72-.257-1.076-.124l-1.217.456a1.125 1.125 0 01-1.369-.49l-1.297-2.247a1.125 1.125 0 01.26-1.431l1.004-.827c.292-.24.437-.613.43-.991a6.932 6.932 0 010-.255c.007-.38-.138-.751-.43-.992l-1.004-.827a1.125 1.125 0 01-.26-1.43l1.297-2.247a1.125 1.125 0 011.37-.491l1.216.456c.356.133.751.072 1.076-.124.072-.044.146-.086.22-.128.332-.183.582-.495.644-.869l.214-1.28z" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M15 12a3 3 0 11-6 0 3 3 0 016 0z" />
          </svg>
          {t('nav.config')}
        </button>
        <button
          onClick={() => onNavigate('browserpanel')}
          className={`nav-item w-full ${activeNav === 'browserpanel' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <circle cx="12" cy="12" r="10" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M2 12h20M12 2a15.3 15.3 0 014 10 15.3 15.3 0 01-4 10 15.3 15.3 0 01-4-10A15.3 15.3 0 0112 2z" />
          </svg>
          {t('nav.browser')}
        </button>
        <button
          onClick={() => onNavigate('logspanel')}
          className={`nav-item w-full ${activeNav === 'logspanel' ? 'active' : ''}`}
        >
          <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M7.5 3.75h9a2.25 2.25 0 012.25 2.25v12a2.25 2.25 0 01-2.25 2.25h-9A2.25 2.25 0 015.25 18V6A2.25 2.25 0 017.5 3.75z" />
            <path strokeLinecap="round" strokeLinejoin="round" d="M8.25 8.25h7.5M8.25 12h7.5M8.25 15.75h4.5" />
          </svg>
          {t('nav.logs')}
        </button>
        {FEATURE_APP_UPDATER_UI && (
          <button
            onClick={() => onNavigate('updatepanel')}
            className={`nav-item w-full ${activeNav === 'updatepanel' ? 'active' : ''}`}
          >
            <svg className="w-4 h-4 nav-item__icon" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M12 16.5V3.75m0 0L7.5 8.25M12 3.75l4.5 4.5" />
              <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 15.75v1.5A2.25 2.25 0 006 19.5h12a2.25 2.25 0 002.25-2.25v-1.5" />
            </svg>
            {t('nav.update')}
          </button>
        )}
      </div>

      <div className="flex-1" />

      {false && <OffloadFilesWidget sessionId={sessionId} />}

      <div className="pt-4 mt-4 border-t border-border text-xs text-text-muted">
        <div className="px-2.5">
          <span>{t('version', { version: appVersion })}</span>
        </div>
      </div>
    </aside>
  );
}
