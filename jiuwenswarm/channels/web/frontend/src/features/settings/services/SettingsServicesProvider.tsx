// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

// dev-stable 适配（context-dev-stable，2026-09-28）：相比 develop 移除
// ExternalCli* 可选服务字段——其依赖的 ExternalCliAgentsSection /
// ExternalCliInstallDialog 属 experimental 模块依赖，本次未迁入。

import { createContext, useContext, useEffect, useMemo, useRef, type ReactNode } from 'react';
import type { WebConnectionState } from '../../../types';
import type { SettingsRequest } from './settingsContract';
import { SettingsSaveQueue } from './SettingsSaveQueue';
import { SettingsUnsavedChangesRegistry } from './SettingsUnsavedChangesRegistry';

export type SettingsServices = {
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  saveQueue: SettingsSaveQueue;
  unsavedChanges: SettingsUnsavedChangesRegistry;
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
