import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import type { WebConnectionState } from '../../../types';
import type { SettingsRequest } from './settingsContract';
import type {
  CodexDependencyInstallStatus,
  ExternalCliAgentKind,
  ExternalCliDetectResult,
} from '../../../components/ExternalCliAgentsSection';
import { SettingsSaveQueue } from './SettingsSaveQueue';
import { SettingsUnsavedChangesRegistry } from './SettingsUnsavedChangesRegistry';

export type SettingsServices = {
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  saveQueue: SettingsSaveQueue;
  unsavedChanges: SettingsUnsavedChangesRegistry;
  onDetectExternalCli?: (agent: ExternalCliAgentKind, path?: string) => Promise<ExternalCliDetectResult>;
  onSelectExternalCliPath?: (agent: ExternalCliAgentKind, initialPath?: string) => Promise<string | null>;
  onGetCodexDependencyInstallStatus?: () => Promise<CodexDependencyInstallStatus>;
};

const SettingsServicesContext = createContext<SettingsServices | null>(null);
export function SettingsServicesProvider({
  children,
  onHasChangesChange,
  ...services
}: Omit<SettingsServices, 'saveQueue' | 'unsavedChanges'> & {
  children: ReactNode;
  onHasChangesChange?: (hasChanges: boolean) => void;
}) {
  const saveQueueRef = useRef<SettingsSaveQueue | null>(null);
  const changesRef = useRef<SettingsUnsavedChangesRegistry | null>(null);
  if (!saveQueueRef.current) saveQueueRef.current = new SettingsSaveQueue();
  if (!changesRef.current) changesRef.current = new SettingsUnsavedChangesRegistry();
  const value = useMemo(
    () => ({ ...services, saveQueue: saveQueueRef.current!, unsavedChanges: changesRef.current! }),
    [
      services.connectionState,
      services.isConnected,
      services.onDetectExternalCli,
      services.onGetCodexDependencyInstallStatus,
      services.onSelectExternalCliPath,
      services.request,
    ],
  );
  useEffect(() => {
    onHasChangesChange?.(value.unsavedChanges.hasChanges());
    return value.unsavedChanges.subscribe(() => onHasChangesChange?.(value.unsavedChanges.hasChanges()));
  }, [onHasChangesChange, value.unsavedChanges]);
  useEffect(() => () => onHasChangesChange?.(false), [onHasChangesChange]);
  return <SettingsServicesContext.Provider value={value}>{children}</SettingsServicesContext.Provider>;
}
export function useSettingsServices(): SettingsServices {
  const services = useContext(SettingsServicesContext);
  if (!services) throw new Error('SettingsServicesProvider is required');
  return services;
}
