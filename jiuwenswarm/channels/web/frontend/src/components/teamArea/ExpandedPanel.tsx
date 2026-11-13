import { useCallback, useEffect, useId, type ReactNode } from 'react';
import { useFullscreenPanel } from '../../hooks';
import { useSessionArtifactsCount } from '../ArtifactsPanel';
import { ArtifactExpandedPanel } from '../ArtifactsPanel';
import { ExpandedPanelTabs, useExpandedPanelTabs, type PanelTabItem } from './ExpandedPanelTabs';
import { OrgInfoPanel } from './OrgInfoPanel';
import { useTeamOrganizationUiEnabled } from '../../features/teamOrganization/featureConfig';
import { useSessionStore, useChatStore } from '../../stores';
import { useTranslation } from 'react-i18next';
import { Building2 } from 'lucide-react';

export interface ExpandedPanelContribution extends PanelTabItem {
  content: ReactNode;
}

export interface ExpandedPanelProps {
  activeTab: string;
  onTabChange: (tab: string) => void;
  onTabClose?: (tab: string) => void;
  onCollapse: () => void;
  shouldFullscreen?: boolean;
  reviewPanel?: ReactNode;
  selectedArtifactId?: string;
  onArtifactSelect?: (artifactId: string) => void;
  middleTab: { key: string; label: string; icon: ReactNode };
  showMiddleTab: boolean;
  showBrowserTab?: boolean;
  resolveActiveTab: (activeTab: string, artifactsCount: number, reviewPanel?: ReactNode) => string;
  renderMiddleTabContent: () => ReactNode;
  renderPlanningContent: () => ReactNode;
  renderBrowserContent?: () => ReactNode;
  additionalPanels?: ExpandedPanelContribution[];
  testIdPrefix?: string;
}

export function ExpandedPanel({
  activeTab,
  onTabChange,
  onTabClose,
  onCollapse,
  shouldFullscreen,
  reviewPanel,
  selectedArtifactId,
  onArtifactSelect,
  middleTab,
  showMiddleTab,
  showBrowserTab,
  resolveActiveTab,
  renderMiddleTabContent,
  renderPlanningContent,
  renderBrowserContent,
  additionalPanels = [],
  testIdPrefix = 'tool-panel',
}: ExpandedPanelProps) {
  const { t } = useTranslation();
  const organizationUiEnabled = useTeamOrganizationUiEnabled();
  const activeSessionId = useChatStore((state) => state.activeSessionId);
  const mode = useSessionStore((state) => state.runtimes[activeSessionId ?? '']?.mode);
  const showOrganization = organizationUiEnabled && mode === 'team';
  const tabPanelId = useId();
  const artifactsCount = useSessionArtifactsCount();
  const {
    ref: fullscreenRef,
    isFullscreen,
    toggle: toggleFullscreen,
    enter: enterFullscreen,
    exit: exitFullscreen,
  } = useFullscreenPanel<HTMLDivElement>();

  useEffect(() => {
    if (shouldFullscreen) {
      enterFullscreen();
    } else {
      exitFullscreen();
    }
  }, [shouldFullscreen, enterFullscreen, exitFullscreen]);

  const handleToggleFullscreen = useCallback(() => {
    if (shouldFullscreen) {
      onCollapse();
    } else {
      toggleFullscreen();
    }
  }, [shouldFullscreen, onCollapse, toggleFullscreen]);

  const selectedPanel = additionalPanels.find((panel) => panel.key === activeTab);
  const resolvedTab = selectedPanel?.key ?? (activeTab === 'organization' && showOrganization ? 'organization' :
    resolveActiveTab(activeTab === 'organization' ? 'planning' : activeTab, artifactsCount, reviewPanel));

  const tabs = useExpandedPanelTabs({ middleTab, showMiddleTab, artifactsCount, reviewPanel, showBrowserTab });
<<<<<<< HEAD
  if (showOrganization) tabs.push({ key: 'organization', label: t('team.org.tab') });
  tabs.push(...additionalPanels);
||||||| e487e9a30
  if (showOrganization) tabs.push({ key: 'organization', label: t('team.org.tab') });
=======
  if (showOrganization) tabs.push({
    key: 'organization',
    label: t('team.org.tab'),
    icon: <Building2 size={16} aria-hidden="true" />,
  });
>>>>>>> origin/develop

  return (
    <div
      ref={fullscreenRef}
      data-testid={`${testIdPrefix}-expanded-body`}
      className="flex h-full flex-col overflow-hidden bg-card"
    >
      <ExpandedPanelTabs
        tabs={tabs}
        activeTab={resolvedTab}
        onTabChange={onTabChange}
        onTabClose={onTabClose}
        onCollapse={onCollapse}
        onToggleFullscreen={handleToggleFullscreen}
        isFullscreen={isFullscreen}
        testIdPrefix={testIdPrefix}
        idPrefix={tabPanelId}
      />

      <div
        data-testid={`${testIdPrefix}-expanded-content`}
        id={`${tabPanelId}-panel`}
        className="flex min-h-0 flex-1 overflow-hidden"
        role="tabpanel"
        aria-labelledby={`${tabPanelId}-${encodeURIComponent(resolvedTab)}`}
      >
        {selectedPanel ? selectedPanel.content : resolvedTab === 'organization' && showOrganization ? <OrgInfoPanel /> : resolvedTab === middleTab.key ? (
          renderMiddleTabContent()
        ) : resolvedTab === 'artifacts' ? (
          <ArtifactExpandedPanel
            selectedArtifactId={selectedArtifactId}
            onSelectArtifact={onArtifactSelect ?? (() => {})}
          />
        ) : resolvedTab === 'review' && reviewPanel ? (
          <div
            data-testid={`${testIdPrefix}-review-pane`}
            data-variant="review"
            className="flex min-w-0 flex-1 overflow-hidden"
          >
            {reviewPanel}
          </div>
        ) : resolvedTab === 'browser' && renderBrowserContent ? (
          renderBrowserContent()
        ) : (
          renderPlanningContent()
        )}
      </div>
    </div>
  );
}
