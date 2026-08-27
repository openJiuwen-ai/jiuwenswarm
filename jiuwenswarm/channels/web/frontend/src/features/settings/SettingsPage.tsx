import type { WebConnectionState } from '../../types';
import type { SettingsRequest } from './services/settingsContract';
import type {
  CodexDependencyInstallStatus,
  ExternalCliAgentKind,
  ExternalCliDetectResult,
} from '../../components/ExternalCliAgentsSection';
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
  onDetectExternalCli,
  onSelectExternalCliPath,
  onGetCodexDependencyInstallStatus,
  initialModuleId,
}: {
  definition: SettingsPageDefinition;
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  onHasChangesChange?: (hasChanges: boolean) => void;
  onDetectExternalCli?: (agent: ExternalCliAgentKind, path?: string) => Promise<ExternalCliDetectResult>;
  onSelectExternalCliPath?: (agent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  onGetCodexDependencyInstallStatus?: () => Promise<CodexDependencyInstallStatus>;
  initialModuleId?: SettingsModuleTarget;
}) {
  return (
    <SettingsServicesProvider
      isConnected={isConnected}
      connectionState={connectionState}
      request={request}
      onHasChangesChange={onHasChangesChange}
      onDetectExternalCli={onDetectExternalCli}
      onSelectExternalCliPath={onSelectExternalCliPath}
      onGetCodexDependencyInstallStatus={onGetCodexDependencyInstallStatus}
    >
      <SettingsPageLayout definition={definition} initialModuleId={initialModuleId} />
    </SettingsServicesProvider>
  );
}
