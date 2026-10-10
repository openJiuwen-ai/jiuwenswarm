import { useTranslation } from 'react-i18next';
import { LoadingSpinner } from '../ui/LoadingSpinner/LoadingSpinner';
import FrameTimeIcon from '../../assets/work-mode/frame-time.svg?react';
import StatusSuccessIcon from '../../assets/work-mode/status-success.svg?react';
import WarningCircleIcon from '../../assets/work-mode/warning-circle.svg?react';
import CancelledIcon from '../../assets/work-mode/已取消.svg?react';
import { getSubagentStatusLabelKey, getSubagentStatusTone } from '../../features/subagent/subagentStatusPresentation';
import type { SubagentClosedReason, SubagentStatus, SubagentTurnOutcome } from '../../types/subagent';

export function SubagentStatusIcon({
  status,
  closedReason,
  turnOutcome,
  className = 'h-4 w-4',
}: {
  status: SubagentStatus;
  closedReason?: SubagentClosedReason | null;
  turnOutcome?: SubagentTurnOutcome | null;
  className?: string;
}) {
  const { t } = useTranslation();
  const tone = getSubagentStatusTone(status, closedReason, turnOutcome);
  const label = t(getSubagentStatusLabelKey(status, closedReason, turnOutcome));

  if (tone === 'running') {
    return <LoadingSpinner />;
  }
  if (tone === 'danger') {
    return <WarningCircleIcon className={`${className} shrink-0 text-danger`} aria-label={label} role="img" />;
  }
  if (tone === 'success') {
    return <StatusSuccessIcon className={`${className} shrink-0 text-[var(--color-team-status-completed-icon)]`} aria-label={label} role="img" />;
  }
  if (tone === 'neutral') {
    // 取消态与 team-area StatusIcon 的 cancelled 同形同规：已取消.svg（禁止符+横杠），不传色继承上下文
    return <CancelledIcon className={`${className} shrink-0`} aria-label={label} role="img" />;
  }
  return <FrameTimeIcon className={`${className} shrink-0 text-text-meta`} aria-label={label} role="img" />;
}
