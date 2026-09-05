// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import type { WebConnectionState } from '../../types';
import type { SettingsRequest } from './services/settingsContract';
import type {
  ExternalCliAgentKind,
  ExternalCliDetectResult,
  ExternalCliPendingChoice,
} from '../../components/ExternalCliAgentsSection';
import type { ExternalCliInstallStatuses } from '../../components/ExternalCliInstallDialog';
import { SettingsPageLayout } from './SettingsPageLayout';
import { SettingsServicesProvider } from './services/SettingsServicesProvider';
import type { SettingsPageDefinition } from './registry/types';
import type { SettingsModuleTarget } from './settingsNavigation';

export function SettingsPage({
  definition,
  isConnected,
  connectionState,
  request,
  onHasChangesChange,
  onConfigSaved,
  onDetectExternalCli,
  onSelectExternalCliPath,
  onTrackExternalCliDependencyInstalls,
  externalCliInstallStatuses,
  externalCliInstallBusy,
  onOpenExternalCliInstallDialog,
  externalCliPendingChoices,
  onExternalCliPendingChoicesChange,
  externalCliDetectResults,
  onExternalCliDetectResultsChange,
  initialModuleId,
}: {
  definition: SettingsPageDefinition;
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  onHasChangesChange?: (hasChanges: boolean) => void;
  onConfigSaved?: (updatedKeys: readonly string[]) => Promise<void> | void;
  onDetectExternalCli?: (agent: ExternalCliAgentKind, path?: string) => Promise<ExternalCliDetectResult>;
  onSelectExternalCliPath?: (agent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  onTrackExternalCliDependencyInstalls?: (statuses: ExternalCliInstallStatuses) => void;
  externalCliInstallStatuses?: ExternalCliInstallStatuses;
  externalCliInstallBusy?: boolean;
  onOpenExternalCliInstallDialog?: () => void;
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
    next: Partial<Record<ExternalCliAgentKind, ExternalCliDetectResult>>,
  ) => void;
  initialModuleId?: SettingsModuleTarget;
}) {
  return (
    <SettingsServicesProvider
      isConnected={isConnected}
      connectionState={connectionState}
      request={request}
      onHasChangesChange={onHasChangesChange}
      onConfigSaved={onConfigSaved}
      onDetectExternalCli={onDetectExternalCli}
      onSelectExternalCliPath={onSelectExternalCliPath}
      onTrackExternalCliDependencyInstalls={onTrackExternalCliDependencyInstalls}
      externalCliInstallStatuses={externalCliInstallStatuses}
      externalCliInstallBusy={externalCliInstallBusy}
      onOpenExternalCliInstallDialog={onOpenExternalCliInstallDialog}
      externalCliPendingChoices={externalCliPendingChoices}
      onExternalCliPendingChoicesChange={onExternalCliPendingChoicesChange}
      externalCliDetectResults={externalCliDetectResults}
      onExternalCliDetectResultsChange={onExternalCliDetectResultsChange}
    >
      <SettingsPageLayout definition={definition} initialModuleId={initialModuleId} />
    </SettingsServicesProvider>
  );
}
