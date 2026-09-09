// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import type { WebConnectionState } from '../../../types';
import type { SettingsRequest } from './settingsContract';
import type {
  ExternalCliAgentKind,
  ExternalCliDetectResult,
  ExternalCliPendingChoice,
} from '../../../components/ExternalCliAgentsSection';
import type { ExternalCliInstallStatuses } from '../../../components/ExternalCliInstallDialog';
import { SettingsSaveQueue } from './SettingsSaveQueue';
import { SettingsUnsavedChangesRegistry } from './SettingsUnsavedChangesRegistry';

export type SettingsServices = {
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  saveQueue: SettingsSaveQueue;
  unsavedChanges: SettingsUnsavedChangesRegistry;
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
      services.externalCliDetectResults,
      services.externalCliInstallBusy,
      services.externalCliInstallStatuses,
      services.externalCliPendingChoices,
      services.isConnected,
      services.onConfigSaved,
      services.onDetectExternalCli,
      services.onExternalCliDetectResultsChange,
      services.onExternalCliPendingChoicesChange,
      services.onOpenExternalCliInstallDialog,
      services.onSelectExternalCliPath,
      services.onTrackExternalCliDependencyInstalls,
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
