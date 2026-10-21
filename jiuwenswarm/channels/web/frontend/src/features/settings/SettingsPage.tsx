// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import type { WebConnectionState } from '../../types';
import type { SettingsRequest } from './services/settingsContract';
import type {
  ExternalCliAgentKind,
  ExternalCliDependencyInstallStatus,
  ExternalCliDetectResult,
  ExternalCliPendingChoice,
} from '../../components/ExternalCliAgentsSection';
import type { ExternalCliInstallStatuses } from '../../components/ExternalCliInstallDialog';
import { SettingsPageLayout, type SettingsSidebarControls } from './SettingsPageLayout';
import { SettingsServicesProvider } from './services/SettingsServicesProvider';
import type { SettingsPageDefinition } from './registry/types';
import type { SettingsModuleTarget } from './settingsNavigation';

export function SettingsPage({
  definition,
  isConnected,
  connectionState,
  request,
  onHasChangesChange,
  onDetectExternalCli,
  onSelectExternalCliPath,
  onTrackExternalCliDependencyInstalls,
  externalCliInstallStatuses,
  onGetExternalCliInstallStatus,
  onExternalCliInstallStatusChange,
  externalCliPendingChoices,
  onExternalCliPendingChoicesChange,
  externalCliDetectResults,
  onExternalCliDetectResultsChange,
  initialModuleId,
  sidebar,
}: {
  definition: SettingsPageDefinition;
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  onHasChangesChange?: (hasChanges: boolean) => void;
  onDetectExternalCli?: (agent: ExternalCliAgentKind, path?: string, useBuiltin?: boolean) => Promise<ExternalCliDetectResult>;
  onSelectExternalCliPath?: (agent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  onTrackExternalCliDependencyInstalls?: (statuses: ExternalCliInstallStatuses) => void;
  externalCliInstallStatuses?: ExternalCliInstallStatuses;
  onGetExternalCliInstallStatus?: (
    agent: ExternalCliAgentKind,
  ) => Promise<ExternalCliDependencyInstallStatus>;
  onExternalCliInstallStatusChange?: (agent: ExternalCliAgentKind, status: ExternalCliDependencyInstallStatus) => void;
  externalCliPendingChoices?: Partial<Record<ExternalCliAgentKind, ExternalCliPendingChoice>>;
  onExternalCliPendingChoicesChange?: (
    next:
      | Partial<Record<ExternalCliAgentKind, ExternalCliPendingChoice>>
      | ((current: Partial<Record<ExternalCliAgentKind, ExternalCliPendingChoice>>) => Partial<
          Record<ExternalCliAgentKind, ExternalCliPendingChoice>
        >),
  ) => void;
  externalCliDetectResults?: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>;
  onExternalCliDetectResultsChange?: (
    next:
      | Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>
      | ((current: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>) => Partial<
          Record<ExternalCliAgentKind, ExternalCliDetectResult>
        >),
  ) => void;
  initialModuleId?: SettingsModuleTarget;
  sidebar?: SettingsSidebarControls;
}) {
  return (
    <SettingsServicesProvider
      isConnected={isConnected}
      connectionState={connectionState}
      request={request}
      onHasChangesChange={onHasChangesChange}
      onDetectExternalCli={onDetectExternalCli}
      onSelectExternalCliPath={onSelectExternalCliPath}
      onTrackExternalCliDependencyInstalls={onTrackExternalCliDependencyInstalls}
      externalCliInstallStatuses={externalCliInstallStatuses}
      onGetExternalCliInstallStatus={onGetExternalCliInstallStatus}
      onExternalCliInstallStatusChange={onExternalCliInstallStatusChange}
      externalCliPendingChoices={externalCliPendingChoices}
      onExternalCliPendingChoicesChange={onExternalCliPendingChoicesChange}
      externalCliDetectResults={externalCliDetectResults}
      onExternalCliDetectResultsChange={onExternalCliDetectResultsChange}
    >
      <SettingsPageLayout definition={definition} initialModuleId={initialModuleId} sidebar={sidebar} />
    </SettingsServicesProvider>
  );
}
