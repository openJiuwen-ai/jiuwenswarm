// Copyright (c) Huawei Technologies Co., Ltd. 2026. All rights reserved.

import { useTranslation } from 'react-i18next';
import type { ExternalCliAgentKind, ExternalCliDependencyInstallStatus } from './ExternalCliAgentsSection';
import { Button, Dialog } from './ui';
import { ExternalCliInstallTaskCard, useInstallStatusPolling } from './ExternalCliInstallProgress';
import './ExternalCliInstallDialog.css';

type InstallStatuses = Partial<Record<ExternalCliAgentKind, ExternalCliDependencyInstallStatus>>;

// All agents' install tasks (legacy dialog view): polls each running
// install once per second and renders per-agent progress cards.
export function ExternalCliInstallTasks({
  statuses,
  onGetStatus,
  onStatusChange,
}: {
  statuses: InstallStatuses;
  onGetStatus: (agent: ExternalCliAgentKind) => Promise<ExternalCliDependencyInstallStatus>;
  onStatusChange: (agent: ExternalCliAgentKind, status: ExternalCliDependencyInstallStatus) => void;
}) {
  const entries = (Object.entries(statuses) as [ExternalCliAgentKind, ExternalCliDependencyInstallStatus][]).filter(
    ([, status]) => Boolean(status),
  );
  useInstallStatusPolling(
    entries.filter(([, status]) => status.status === 'running').map(([agent]) => agent),
    onGetStatus,
    onStatusChange,
  );

  return (
    <div className="external-cli-install-dialog__body">
      {entries.map(([agent, status]) => (
        <ExternalCliInstallTaskCard agent={agent} status={status} key={agent} />
      ))}
    </div>
  );
}

export function ExternalCliInstallDialog({
  open,
  statuses,
  onClose,
  onGetStatus,
  onStatusChange,
}: {
  open: boolean;
  statuses: InstallStatuses;
  onClose: () => void;
  onGetStatus: (agent: ExternalCliAgentKind) => Promise<ExternalCliDependencyInstallStatus>;
  onStatusChange: (agent: ExternalCliAgentKind, status: ExternalCliDependencyInstallStatus) => void;
}) {
  const { t } = useTranslation();
  const running = (Object.values(statuses) as (ExternalCliDependencyInstallStatus | undefined)[]).some(
    (status) => status?.status === 'running',
  );

  return (
    <Dialog open={open} titleId="external-cli-install-dialog-title" onCancel={onClose}>
      <div className="external-cli-install-dialog" data-testid="external-cli-install-dialog">
        <header className="external-cli-install-dialog__header">
          <div>
            <h2 id="external-cli-install-dialog-title">{t('config.externalCli.installDialogTitle')}</h2>
            <p>{t('config.externalCli.installDialogDescription')}</p>
          </div>
        </header>
        <ExternalCliInstallTasks statuses={statuses} onGetStatus={onGetStatus} onStatusChange={onStatusChange} />
        <footer className="external-cli-install-dialog__footer">
          <Button onClick={onClose}>
            {running ? t('config.externalCli.installRunInBackground') : t('common.close')}
          </Button>
        </footer>
      </div>
    </Dialog>
  );
}

export type { InstallStatuses as ExternalCliInstallStatuses };
