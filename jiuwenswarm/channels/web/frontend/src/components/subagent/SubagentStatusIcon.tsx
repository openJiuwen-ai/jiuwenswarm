import { useTranslation } from 'react-i18next';
import { CircleAlert } from 'lucide-react';
import LoadingIcon from '../../assets/subagent/loading.svg?react';
import SuccessIcon from '../../assets/subagent/success.svg?react';
import WaitingIcon from '../../assets/subagent/waiting.svg?react';
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
    return <LoadingIcon className={`${className} shrink-0 text-muted animate-spin`} aria-label={label} role="img" />;
  }
  if (tone === 'waiting') {
    return <WaitingIcon className={`${className} shrink-0 text-text-muted`} aria-label={label} role="img" />;
  }
  if (tone === 'danger') {
    return <CircleAlert className={`${className} shrink-0 text-danger`} aria-label={label} role="img" />;
  }
  if (tone === 'success') {
    return <SuccessIcon className={`${className} shrink-0 text-ok`} aria-label={label} role="img" />;
  }
  return <WaitingIcon className={`${className} shrink-0 text-text-muted`} aria-label={label} role="img" />;
}
