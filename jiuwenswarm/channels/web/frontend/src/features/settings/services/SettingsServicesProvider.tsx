// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import type { WebConnectionState } from '../../../types';
import type { SettingsRequest } from './settingsContract';
import type {
  ExternalCliAgentKind,
  ExternalCliDependencyInstallStatus,
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
  onDetectExternalCli?: (agent: ExternalCliAgentKind, path?: string, useBuiltin?: boolean) => Promise<ExternalCliDetectResult>;
  /** Checks which CLI paths exist on disk; keys are the requested paths. */
  onValidateExternalCliPaths?: (paths: string[]) => Promise<Record<string, boolean | null>>;
  /** Server-side prune of one kind's persisted history: only the
   * intersection with the on-disk history is dropped. Returns survivors. */
  onPruneExternalCliPathHistories?: (agent: ExternalCliAgentKind, removePaths: string[]) => Promise<string[]>;
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
      services.externalCliInstallStatuses,
      services.externalCliPendingChoices,
      services.isConnected,
      services.onDetectExternalCli,
      services.onExternalCliDetectResultsChange,
      services.onExternalCliInstallStatusChange,
      services.onExternalCliPendingChoicesChange,
      services.onGetExternalCliInstallStatus,
      services.onSelectExternalCliPath,
      services.onTrackExternalCliDependencyInstalls,
      services.onValidateExternalCliPaths,
      services.onPruneExternalCliPathHistories,
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
