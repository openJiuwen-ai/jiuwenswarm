import { useTranslation } from 'react-i18next';
import FrameTimeIcon from '../../assets/work-mode/frame-time.svg?react';
import { LoadingSpinner } from '../ui/LoadingSpinner/LoadingSpinner';
import { CircleAlert, CircleHelp, CirclePause, Moon, Power, RotateCw } from 'lucide-react';
import { getMemberStatusKey, type TeamMember } from './shared';

/** Shared status presentation for compact member rows in either edition. */
export function MemberExecutionStatus({ member, showIdleStatus = false }: { member: TeamMember; showIdleStatus?: boolean }) {
  const { t } = useTranslation();
  const actualStatus = getMemberStatusKey(member);
  const status = actualStatus === 'running' && showIdleStatus ? 'idle' : actualStatus;
  const icons: Record<string, typeof CircleAlert> = {
    error: CircleAlert,
    restarting: RotateCw,
    dormant: Moon,
    shutdown: Power,
    unstarted: CirclePause,
    unknown: CircleHelp,
  };
  const StatusIcon = icons[status];
  return (
    <span className="flex h-4 w-4 shrink-0 items-center justify-center"
      data-testid="team-area-member-execution-status" data-variant={status}
      title={t(`team.memberStatus.${status}`)} aria-label={t(`team.memberStatus.${status}`)}>
      {status === 'running' ? <LoadingSpinner /> : StatusIcon ? (
        <StatusIcon className={`h-4 w-4 ${status === 'error' ? 'text-warn' : 'text-text-meta'}`} aria-hidden="true" />
      ) : <FrameTimeIcon className="h-4 w-4 text-text-meta" aria-hidden="true" />}
    </span>
  );
}
