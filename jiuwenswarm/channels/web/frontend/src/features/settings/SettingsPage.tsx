// Copyright (c) Huawei Technologies Co., Ltd. 2025-2026. All rights reserved.

// dev-stable 适配（context-dev-stable，2026-09-28）：相比 develop 移除
// ExternalCli* 可选 props——其依赖的 ExternalCliAgentsSection /
// ExternalCliInstallDialog 属 experimental 模块依赖，本次未迁入。

import type { WebConnectionState } from '../../types';
import type { SettingsRequest } from './services/settingsContract';
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
  initialModuleId,
}: {
  definition: SettingsPageDefinition;
  isConnected: boolean;
  connectionState: WebConnectionState;
  request: SettingsRequest;
  onHasChangesChange?: (hasChanges: boolean) => void;
  initialModuleId?: SettingsModuleTarget;
}) {
  return (
    <SettingsServicesProvider
      isConnected={isConnected}
      connectionState={connectionState}
      request={request}
      onHasChangesChange={onHasChangesChange}
    >
      <SettingsPageLayout definition={definition} initialModuleId={initialModuleId} />
    </SettingsServicesProvider>
  );
}
