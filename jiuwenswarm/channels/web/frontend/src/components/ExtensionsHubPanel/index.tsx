/**
 * ExtensionsHubPanel Component
 *
 * Unified panel combining ExtensionsPanel and HarnessPackagePanel
 * with tab switching at the top.
 */

import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { ExtensionsPanel } from '../ExtensionsPanel';
import { HarnessPackagePanel } from '../HarnessPackagePanel';
import './ExtensionsHubPanel.css';

type HubTabKey = 'rails' | 'harnesspkg';

interface TabItem {
  key: HubTabKey;
  label: string;
  icon: JSX.Element;
  hidden?: boolean;
}

interface ExtensionsHubPanelProps {
  sessionId: string;
  isConnected: boolean;
}

export function ExtensionsHubPanel({ sessionId, isConnected }: ExtensionsHubPanelProps) {
  const { t } = useTranslation();
  const [activeTab, setActiveTab] = useState<HubTabKey>('harnesspkg');

  const tabs: TabItem[] = [
    {
      key: 'harnesspkg',
      label: t('nav.harnesspkg', 'Plugins'),
      hidden: false, // Temporarily hidden from web UI, core functionality preserved for future re-enable
      icon: (
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M21 7.5l-9-5.25L3 7.5m18 0l-9 5.25m9-5.25v9l-9 5.25M3 7.5l9 5.25M3 7.5v9l9 5.25m0-9v9" />
        </svg>
      ),
    },
    {
      key: 'rails',
      label: t('nav.rails', 'Extensions'),
      icon: (
        <svg className="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" strokeWidth={1.5}>
          <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6.75h16.5M3.75 12h16.5m-16.5 5.25h16.5" />
        </svg>
      ),
    },
  ];

  const visibleTabs = tabs.filter((tab) => !tab.hidden);

  return (
    <div className="extensions-hub-panel">
      {/* Tab Header - only show when multiple tabs visible */}
      <div className="extensions-hub-panel__header">
        <div className="extensions-hub-panel__tabs">
          {visibleTabs.map((tab) => (
            <button
              type="button"
              key={tab.key}
              onClick={() => setActiveTab(tab.key)}
              className={`extensions-hub-panel__tab ${activeTab === tab.key ? 'extensions-hub-panel__tab--active' : ''}`}
            >
              {tab.icon}
              <span>{tab.label}</span>
            </button>
          ))}
        </div>
      </div>

      {/* Tab Content */}
      <div className="extensions-hub-panel__content">
        {activeTab === 'rails' && (
          <ExtensionsPanel isConnected={isConnected} />
        )}
        {activeTab === 'harnesspkg' && (
          <HarnessPackagePanel sessionId={sessionId} />
        )}
      </div>
    </div>
  );
}