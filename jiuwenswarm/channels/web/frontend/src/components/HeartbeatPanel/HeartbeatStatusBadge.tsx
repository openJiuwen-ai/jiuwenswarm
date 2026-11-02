import { useTranslation } from 'react-i18next';
import type { HeartbeatJobStatus } from '../../types/heartbeat';
import { heartbeatStatusVariant, heartbeatStatusLabelKey, type HeartbeatStatusVariant } from './heartbeatStatusText';
import { RunningIcon, BoldRingIcon } from '../CronPanel/StatusBadge';

const VARIANT_CLASS: Record<HeartbeatStatusVariant, string> = {
  running: 'text-cron-running',
  scheduled: 'text-cron-running',
  paused: 'text-text-muted',
  completed: 'text-text-muted',
  expired: 'text-amber-600',
};

export default function HeartbeatStatusBadge({ status }: { status: HeartbeatJobStatus }) {
  const { t } = useTranslation();
  const variant = heartbeatStatusVariant(status);
  const Icon = variant === 'running' ? RunningIcon : BoldRingIcon;
  return (
    <span className={`inline-flex items-center gap-1.5 text-sm ${VARIANT_CLASS[variant]}`}>
      <Icon />
      {t(heartbeatStatusLabelKey(status))}
    </span>
  );
}
